from utils.logger import setup_logger
from datasets import make_dataloader
from model import make_model
from solver import make_optimizer
from solver.scheduler_factory import create_scheduler
from loss import make_loss
from processor import do_train
import random
import torch
import numpy as np
import os
import argparse
# from timm.scheduler import create_scheduler
from config import cfg
from utils.saver import Saver

def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description="ReID Baseline Training")
    parser.add_argument(
        "--config_file", default="", help="path to config file", type=str
    )

    parser.add_argument("opts", help="Modify config options using the command-line", default=None,
                        nargs=argparse.REMAINDER)
    parser.add_argument("--local_rank", default=0, type=int)
    args = parser.parse_args()

    if args.config_file != "":
        cfg.merge_from_file(args.config_file)
    cfg.merge_from_list(args.opts)
    cfg.freeze()

    set_seed(cfg.SOLVER.SEED)

    if cfg.MODEL.DIST_TRAIN:
        torch.cuda.set_device(args.local_rank)

    output_dir = cfg.OUTPUT_DIR
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    logger = setup_logger("pit", output_dir, if_train=True)
    logger.info("Saving model in the path :{}".format(cfg.OUTPUT_DIR))
    logger.info(args)

    if args.config_file != "":
        logger.info("Loaded configuration file {}".format(args.config_file))
        with open(args.config_file, 'r') as cf:
            config_str = "\n" + cf.read()
            logger.info(config_str)
    logger.info("Running with config:\n{}".format(cfg))

    if cfg.MODEL.DIST_TRAIN:
        torch.distributed.init_process_group(backend='nccl', init_method='env://')

    os.environ['CUDA_VISIBLE_DEVICES'] = cfg.MODEL.DEVICE_ID
    train_loader, val_loader, num_classes, camera_num, view_num = make_dataloader(cfg)

    if cfg.DATASETS.NAMES in ['ilids']:
        num_trials = 10
    elif cfg.DATASETS.NAMES in ['polarbearvidid']:
        num_trials = 5
    else:
        num_trials = 1
    cmcs, mAPs = [], []
    for i in range(num_trials):
        output_dir = cfg.OUTPUT_DIR + '/' + str(i + 1)
        test_weight = cfg.TEST.WEIGHT + '/' + str(i + 1)

        saver = Saver(output_dir, f'tensorboard')

        model = make_model(cfg, num_class=num_classes, camera_num=camera_num, view_num = view_num)
        
        test = os.path.isdir(test_weight)
        if test:
            model.load_param(os.path.join(test_weight, 'transformer_' + str(120) + '.pth'))

        computation_complexity = False
        if computation_complexity:
            from thop import profile
            import torch

            x = torch.randn(1, 1, 3, 256, 128)
            label = torch.randn(16).long()
            cam_label = torch.randn(1).long().clamp(0, 1)
            view_label = torch.randn(1).long().clamp(1, 1)
            macs, params = profile(model, inputs=(x,label, cam_label, view_label))
            print(macs);    print(params);

        loss_func, center_criterion = make_loss(cfg, num_classes=num_classes)

        optimizer, optimizer_center = make_optimizer(cfg, model, center_criterion)

        scheduler = create_scheduler(cfg, optimizer)

        num_query = len(val_loader[i].dataset)

        cmc, mAP = do_train(
            cfg,
            model,
            center_criterion,
            train_loader[i],
            val_loader[i],
            optimizer,
            optimizer_center,
            scheduler,
            loss_func,
            num_query, args.local_rank, saver, i, test
        )
        cmcs.append(cmc)
        mAPs.append(mAP)

    logger.info("{} folds average:".format(num_trials))
    
    if num_trials > 1:
        mAP = np.stack(mAPs).mean(axis=0)
        mAP_std = np.stack(mAPs).std(axis=0)
        logger.info("mAP: {:.3%}, Standard deviation: {:.3%}".format(mAP, mAP_std))
        cmc = np.stack(cmcs).mean(axis=0)
    else:
        mAP = mAPs[0]
        logger.info("mAP: {:.3%}".format(mAP))
        cmc = cmcs[0]

    for r in [1, 5, 10, 20]:
        logger.info("CMC curve, Rank-{:<3}:{:.3%}".format(r, cmc[r - 1]))

