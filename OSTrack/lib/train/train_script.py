import os
# loss function related
from lib.utils.box_ops import giou_loss
from torch.nn.functional import l1_loss
from torch.nn import BCEWithLogitsLoss
# train pipeline related
from lib.train.trainers import LTRTrainer
# distributed training related
from torch.nn.parallel import DistributedDataParallel as DDP
# some more advanced functions
from .base_functions import *
# network related
from lib.models.ostrack import build_ostrack
# forward propagation related
from lib.train.actors import OSTrackActor
# for import modules
import importlib

from ..utils.focal_loss import FocalLoss


def run(settings):
    settings.description = 'Training script for STARK-S, STARK-ST stage1, and STARK-ST stage2'

    # update the default configs with config file
    if not os.path.exists(settings.cfg_file):
        raise ValueError("%s doesn't exist." % settings.cfg_file)
    config_module = importlib.import_module("lib.config.%s.config" % settings.script_name)
    cfg = config_module.cfg
    config_module.update_config_from_file(settings.cfg_file)
    if settings.local_rank in [-1, 0]:
        print("New configuration is shown below.")
        for key in cfg.keys():
            print("%s configuration:" % key, cfg[key])
            print('\n')

    # update settings based on cfg
    update_settings(settings, cfg)

    # Record the training log
    log_dir = os.path.join(settings.save_dir, 'logs')
    if settings.local_rank in [-1, 0]:
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
    settings.log_file = os.path.join(log_dir, "%s-%s.log" % (settings.script_name, settings.config_name))

    # Build dataloaders
    loader_train, loader_val = build_dataloaders(cfg, settings)

    if "RepVGG" in cfg.MODEL.BACKBONE.TYPE or "swin" in cfg.MODEL.BACKBONE.TYPE or "LightTrack" in cfg.MODEL.BACKBONE.TYPE:
        cfg.ckpt_dir = settings.save_dir

    # Create network
    if settings.script_name == "ostrack":
        net = build_ostrack(cfg)
    else:
        raise ValueError("illegal script name")

    # wrap networks to distributed one
    
    if settings.child_train:
  
        ckpt_path = cfg.MODEL.PRETRAIN_FILE
        ckpt = torch.load(ckpt_path)
        missing_keys, unexpected_keys = net.load_state_dict(ckpt["net"], strict=True)
        print('Load pretrained model from: ' + cfg.MODEL.PRETRAIN_FILE)
        
        assert cfg.TRAIN.pruning_type==1 or cfg.TRAIN.pruning_type==2
        
        if cfg.TRAIN.pruning_type==1:
            thresh = net.backbone.compress(cfg.TRAIN.budget_attention, cfg.TRAIN.budget_mlp, 0.1)
            print('Pruning Type: Naive')
        if cfg.TRAIN.pruning_type==2:
            thresh = net.backbone.compress_layerwise(cfg.TRAIN.budget_attention, cfg.TRAIN.budget_mlp, 0.1)
            print('Pruning Type: Layerwise')
            
        print('Threshold', thresh)
        print('Attention budget = ', cfg.TRAIN.budget_attention)
        print('MLP budget = ', cfg.TRAIN.budget_mlp)
        print('Loaded weight from ',ckpt_path)
        
        
    net.cuda()
    if settings.local_rank != -1:
        # net = torch.nn.SyncBatchNorm.convert_sync_batchnorm(net)  # add syncBN converter
        net = DDP(net, device_ids=[settings.local_rank], find_unused_parameters=True)
        settings.device = torch.device("cuda:%d" % settings.local_rank)
    else:
        settings.device = torch.device("cuda:0")
    settings.deep_sup = getattr(cfg.TRAIN, "DEEP_SUPERVISION", False)
    settings.distill = getattr(cfg.TRAIN, "DISTILL", False)
    settings.distill_loss_type = getattr(cfg.TRAIN, "DISTILL_LOSS_TYPE", "KL")
    
    settings.sparsity_train = cfg.TRAIN.sparsity_train
    settings.uniform_search = cfg.TRAIN.uniform_search
    settings.head_search = cfg.TRAIN.head_search
    settings.w1 = cfg.TRAIN.w1
    settings.w2 = cfg.TRAIN.w2
    settings.w3 = cfg.TRAIN.w3
    settings.wandb_key = cfg.TRAIN.wandb_key
    
    # Loss functions and Actors
    if settings.script_name == "ostrack":
        focal_loss = FocalLoss()
        objective = {'giou': giou_loss, 'l1': l1_loss, 'focal': focal_loss, 'cls': BCEWithLogitsLoss()}
        loss_weight = {'giou': cfg.TRAIN.GIOU_WEIGHT, 'l1': cfg.TRAIN.L1_WEIGHT, 'focal': 1., 'cls': 1.0}
        actor = OSTrackActor(net=net, objective=objective, loss_weight=loss_weight, settings=settings, cfg=cfg)
    else:
        raise ValueError("illegal script name")

    # if cfg.TRAIN.DEEP_SUPERVISION:
    #     raise ValueError("Deep supervision is not supported now.")

    # Optimizer, parameters, and learning rates
    optimizer, lr_scheduler = get_optimizer_scheduler(net, cfg)
    use_amp = getattr(cfg.TRAIN, "AMP", False)
    trainer = LTRTrainer(actor, [loader_train, loader_val], optimizer, settings, lr_scheduler, use_amp=use_amp)

    # train process
    trainer.train(cfg.TRAIN.EPOCH, load_latest=True, fail_safe=True)