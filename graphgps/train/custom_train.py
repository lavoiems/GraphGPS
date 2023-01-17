import logging
import time
import copy

import numpy as np
import torch
from torch_geometric.graphgym.checkpoint import load_ckpt, save_ckpt, clean_ckpt
from torch_geometric.graphgym.config import cfg
from torch_geometric.graphgym.loss import compute_loss
from torch_geometric.graphgym.register import register_train
from torch_geometric.graphgym.utils.epoch import is_eval_epoch, is_ckpt_epoch
from torch_geometric.graphgym.optim import create_optimizer, \
    create_scheduler, OptimizerConfig

from graphgps.loss.subtoken_prediction_loss import subtoken_cross_entropy
from graphgps.utils import cfg_to_dict, flatten_dict, make_wandb_name
from graphgps.network.gps_sem_model import GPSSEMModel
from graphgps.optimizer.extra_optimizers import ExtendedSchedulerConfig


def new_optimizer_config(cfg):
    return OptimizerConfig(optimizer=cfg.optim.optimizer,
                           base_lr=cfg.optim.base_lr,
                           weight_decay=cfg.optim.weight_decay,
                           momentum=cfg.optim.momentum)


def new_scheduler_config(cfg):
    return ExtendedSchedulerConfig(
        scheduler=cfg.optim.scheduler,
        steps=cfg.optim.steps, lr_decay=cfg.optim.lr_decay,
        max_epoch=cfg.optim.max_epoch, reduce_factor=cfg.optim.reduce_factor,
        schedule_patience=cfg.optim.schedule_patience, min_lr=cfg.optim.min_lr,
        num_warmup_epochs=cfg.optim.num_warmup_epochs,
        train_mode=cfg.train.mode, eval_period=cfg.train.eval_period)


def train_epoch(logger, loader, model, optimizer, scheduler, batch_accumulation):
    model.train()
    optimizer.zero_grad()
    time_start = time.time()
    for iter, batch in enumerate(loader):
        batch.split = 'train'
        batch.to(torch.device(cfg.device))
        pred, true = model(batch)
        if cfg.dataset.name == 'ogbg-code2':
            loss, pred_score = subtoken_cross_entropy(pred, true)
            _true = true
            _pred = pred_score
        else:
            loss, pred_score = compute_loss(pred, true)
            _true = true.detach().to('cpu', non_blocking=True)
            _pred = pred_score.detach().to('cpu', non_blocking=True)
        loss.backward()
        # Parameters update after accumulating gradients for given num. batches.
        if ((iter + 1) % batch_accumulation == 0) or (iter + 1 == len(loader)):
            if cfg.optim.clip_grad_norm:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad()
        logger.update_stats(true=_true,
                            pred=_pred,
                            loss=loss.detach().cpu().item(),
                            lr=scheduler.get_last_lr()[0],
                            time_used=time.time() - time_start,
                            params=cfg.params,
                            dataset_name=cfg.dataset.name)
        time_start = time.time()


def head_epoch(logger, loader, model, optimizer, scheduler, batch_accumulation, L, V, tau):
    model.train()
    optimizer.zero_grad()
    time_start = time.time()
    for iter, batch in enumerate(loader):
        batch.split = 'train'
        batch.to(torch.device(cfg.device))
        with torch.no_grad():
            out = model.forward_unnormalize_sem(batch)
            o = out.x
            o = o.view(-1, L, V)
            o = torch.nn.functional.softmax(o/tau, -1)
            out.x = o.view(-1, L*V)

        pred, true = model.post_mp(out.detach())
        if cfg.dataset.name == 'ogbg-code2':
            loss, pred_score = subtoken_cross_entropy(pred, true)
            _true = true
            _pred = pred_score
        else:
            loss, pred_score = compute_loss(pred, true)
            _true = true.detach().to('cpu', non_blocking=True)
            _pred = pred_score.detach().to('cpu', non_blocking=True)
        loss.backward()
        # Parameters update after accumulating gradients for given num. batches.
        if ((iter + 1) % batch_accumulation == 0) or (iter + 1 == len(loader)):
            if cfg.optim.clip_grad_norm:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            optimizer.zero_grad()
        logger.update_stats(true=_true,
                            pred=_pred,
                            loss=loss.detach().cpu().item(),
                            lr=scheduler.get_last_lr()[0],
                            time_used=time.time() - time_start,
                            params=cfg.params,
                            dataset_name=cfg.dataset.name)
        time_start = time.time()


def distill_epoch(logger, loader, student, teacher, optimizer, scheduler, batch_accumulation, L, V, tau):
    student.train()
    teacher.eval()
    total_loss = 0
    #time_start = time.time()
    for batch in loader:
        batch.split = 'train'
        batch.to(torch.device(cfg.device))
        o_student = student.forward_unnormalize_sem(batch.clone()).x # Logits
        o_teacher = teacher.forward_unnormalize_sem(batch.clone()).x # Logits

        o_student = o_student.view(-1, L, V)
        o_teacher = o_teacher.view(-1, L, V)

        sampler = torch.distributions.categorical.Categorical(torch.nn.Softmax(-1)(o_teacher))
        teach_label = sampler.sample().long()
        # teach_label = o_teacher.argmax(-1)
        loss = torch.nn.functional.cross_entropy(o_student.reshape(-1,V)/tau, teach_label.reshape(-1,))

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total_loss += loss.item()

    dist = torch.nn.functional.softmax(o_student, -1)
    H = -torch.sum(dist * torch.log(dist), -1).mean()
    total_loss /= len(loader)

    return {'loss': total_loss, 'entropy': H}

        # logger.update_stats(true=_true,
        #                     pred=_pred,
        #                     loss=loss.detach().cpu().item(),
        #                     lr=scheduler.get_last_lr()[0],
        #                     time_used=time.time() - time_start,
        #                     params=cfg.params,
        #                     dataset_name=cfg.dataset.name)
        # time_start = time.time()


@torch.no_grad()
def eval_epoch(logger, loader, model, split='val'):
    model.eval()
    time_start = time.time()
    for batch in loader:
        batch.split = split
        batch.to(torch.device(cfg.device))
        if cfg.gnn.head == 'inductive_edge':
            pred, true, extra_stats = model(batch)
        else:
            pred, true = model(batch)
            extra_stats = {}
        if cfg.dataset.name == 'ogbg-code2':
            loss, pred_score = subtoken_cross_entropy(pred, true)
            _true = true
            _pred = pred_score
        else:
            loss, pred_score = compute_loss(pred, true)
            _true = true.detach().to('cpu', non_blocking=True)
            _pred = pred_score.detach().to('cpu', non_blocking=True)
        logger.update_stats(true=_true,
                            pred=_pred,
                            loss=loss.detach().cpu().item(),
                            lr=0, time_used=time.time() - time_start,
                            params=cfg.params,
                            dataset_name=cfg.dataset.name,
                            **extra_stats)
        time_start = time.time()


@register_train('custom')
def custom_train(loggers, loaders, model, optimizer, scheduler):
    """
    Customized training pipeline.

    Args:
        loggers: List of loggers
        loaders: List of loaders
        model: GNN model
        optimizer: PyTorch optimizer
        scheduler: PyTorch learning rate scheduler

    """
    total_epoch = 0
    if cfg.train.auto_resume:
        total_epoch = load_ckpt(model, optimizer, scheduler,
                                cfg.train.epoch_resume)
    if total_epoch == cfg.optim.num_iteration*(cfg.optim.max_epoch+cfg.optim.distill_epoch):
        logging.info('Checkpoint found, Task already done')
    else:
        logging.info('Start from epoch %s', total_epoch)

    if cfg.wandb.use:
        try:
            import wandb
        except:
            raise ImportError('WandB is not installed.')
        if cfg.wandb.name == '':
            wandb_name = make_wandb_name(cfg)
        else:
            wandb_name = cfg.wandb.name
        run = wandb.init(entity=cfg.wandb.entity, project=cfg.wandb.project,
                         name=wandb_name)
        run.config.update(cfg_to_dict(cfg))

    num_splits = len(loggers)
    split_names = ['val', 'test']
    full_epoch_times = []
    perf = [[] for _ in range(num_splits)]

    # Allow training of only the last N layers
    for layer in list(model.children())[-2:]:
        for param in layer.parameters():
            param.requires_grad = True
    for layer in list(list(model.children())[-3].children())[-cfg.optim.N_train_gps:]:
        for param in layer.parameters():
            param.requires_grad = True

    start_iteration = total_epoch // (cfg.optim.max_epoch + cfg.optim.distill_epoch)
    start_epoch = total_epoch % (cfg.optim.max_epoch + cfg.optim.distill_epoch)
    for cur_iteration in range(start_iteration, cfg.optim.num_iteration):

        if cfg.optim.distill_epoch == 0 or cur_iteration != 0:
            # Interaction phase
            for cur_epoch in range(start_epoch, cfg.optim.max_epoch):
                start_time = time.perf_counter()
                train_epoch(loggers[0], loaders[0], model, optimizer, scheduler,
                            cfg.optim.batch_accumulation)
                perf[0].append(loggers[0].write_epoch(total_epoch))

                if is_eval_epoch(total_epoch):
                    for i in range(1, num_splits):
                        eval_epoch(loggers[i], loaders[i], model,
                                   split=split_names[i - 1])
                        perf[i].append(loggers[i].write_epoch(total_epoch))
                else:
                    for i in range(1, num_splits):
                        perf[i].append(perf[i][-1])

                val_perf = perf[1]
                if cfg.optim.scheduler == 'reduce_on_plateau':
                    scheduler.step(val_perf[-1]['loss'])
                else:
                    scheduler.step()
                full_epoch_times.append(time.perf_counter() - start_time)
                # Checkpoint with regular frequency (if enabled).
                if cfg.train.enable_ckpt and not cfg.train.ckpt_best \
                        and is_ckpt_epoch(total_epoch):
                    save_ckpt(model, optimizer, scheduler, total_epoch)

                if cfg.wandb.use:
                    run.log(flatten_dict(perf), step=total_epoch)

                # Log current best stats on eval epoch.
                if is_eval_epoch(total_epoch):
                    best_epoch = np.array([vp['loss'] for vp in val_perf]).argmin()
                    best_train = best_val = best_test = ""
                    if cfg.metric_best != 'auto':
                        # Select again based on val perf of `cfg.metric_best`.
                        m = cfg.metric_best
                        best_epoch = getattr(np.array([vp[m] for vp in val_perf]),
                                             cfg.metric_agg)()
                        if m in perf[0][best_epoch]:
                            best_train = f"train_{m}: {perf[0][best_epoch][m]:.4f}"
                        else:
                            # Note: For some datasets it is too expensive to compute
                            # the main metric on the training set.
                            best_train = f"train_{m}: {0:.4f}"
                        best_val = f"val_{m}: {perf[1][best_epoch][m]:.4f}"
                        best_test = f"test_{m}: {perf[2][best_epoch][m]:.4f}"

                        if cfg.wandb.use:
                            bstats = {"best/epoch": best_epoch}
                            for i, s in enumerate(['train', 'val', 'test']):
                                bstats[f"best/{s}_loss"] = perf[i][best_epoch]['loss']
                                if m in perf[i][best_epoch]:
                                    bstats[f"best/{s}_{m}"] = perf[i][best_epoch][m]
                                    run.summary[f"best_{s}_perf"] = \
                                        perf[i][best_epoch][m]
                                for x in ['hits@1', 'hits@3', 'hits@10', 'mrr']:
                                    if x in perf[i][best_epoch]:
                                        bstats[f"best/{s}_{x}"] = perf[i][best_epoch][x]
                            run.log(bstats, step=total_epoch)
                            run.summary["full_epoch_time_avg"] = np.mean(full_epoch_times)
                            run.summary["full_epoch_time_sum"] = np.sum(full_epoch_times)
                    # Checkpoint the best epoch params (if enabled).
                    if cfg.train.enable_ckpt and cfg.train.ckpt_best and \
                            best_epoch == total_epoch:
                        save_ckpt(model, optimizer, scheduler, total_epoch)
                        if cfg.train.ckpt_clean:  # Delete old ckpt each time.
                            clean_ckpt()
                    logging.info(
                        f"> IL Iteration {cur_iteration} at Epoch {total_epoch}: took {full_epoch_times[-1]:.1f}s "
                        f"(avg {np.mean(full_epoch_times):.1f}s) | "
                        f"Best so far: epoch {best_epoch}\t"
                        f"train_loss: {perf[0][best_epoch]['loss']:.4f} {best_train}\t"
                        f"val_loss: {perf[1][best_epoch]['loss']:.4f} {best_val}\t"
                        f"test_loss: {perf[2][best_epoch]['loss']:.4f} {best_test}"
                    )
                    if hasattr(model, 'trf_layers'):
                        # Log SAN's gamma parameter values if they are trainable.
                        for li, gtl in enumerate(model.trf_layers):
                            if torch.is_tensor(gtl.attention.gamma) and \
                                    gtl.attention.gamma.requires_grad:
                                logging.info(f"    {gtl.__class__.__name__} {li}: "
                                             f"gamma={gtl.attention.gamma.item()}")
                total_epoch += 1
        logging.info(f"Avg time per epoch: {np.mean(full_epoch_times):.2f}s")
        logging.info(f"Total train loop time: {np.sum(full_epoch_times) / 3600:.2f}h")

        # Evaluating model before distilling.
        # for i in range(1, num_splits):
        #     eval_epoch(loggers[i], loaders[i], model,
        #                split=split_names[i - 1])
            # perf[i].append(loggers[i].write_epoch(total_epoch))
        # logging.info(f'Val loss: {perf[1][-1]["loss"]:.4f} \t Test loss: {perf[2][-1]["loss"]:.4f}')

        start_epoch = start_epoch if start_epoch > cfg.optim.max_epoch else 0
        # Distillation

        new_model = GPSSEMModel(model.dim_in, model.dim_out).cuda()
        # new_model = copy.deepcopy(model)
        # Re-initialize only last N layers
        # for layer in list(new_model.children())[-2:]:
        #     for module in list(layer.modules())[::-1]:
        #         if hasattr(module, 'reset_parameters'):
        #             module.reset_parameters()
        # for layer in list(list(new_model.children())[-3].children())[-cfg.optim.N_train_gps:]:
        #     for module in list(layer.modules())[::-1]:
        #         if hasattr(module, 'reset_parameters'):
        #             module.reset_parameters()

        distill_cfg = copy.deepcopy(cfg)
        # distill_cfg.optim.weight_decay = 0.0001
        optimizer = create_optimizer(new_model.parameters(),
                                     new_optimizer_config(distill_cfg))
        scheduler = create_scheduler(optimizer, new_scheduler_config(cfg))
        for cur_epoch in range(start_epoch, cfg.optim.distill_epoch):
            logging.info(f'Distilling model. Epoch {cur_epoch} / {cfg.optim.distill_epoch}')
            # Re-initialize a new student. Old student becomes teacher
            log = distill_epoch(logging, loaders[0], new_model, model, optimizer, scheduler, cfg.optim.batch_accumulation, cfg.sem.L, cfg.sem.V, cfg.sem.distill_tau)
            loss, entropy = log['loss'], log['entropy']
            logging.info(f'Distill loss: {loss}, entropy: {entropy}')
            # if is_eval_epoch(total_epoch):
            #     for i in range(1, num_splits):
            #         eval_epoch(loggers[i], loaders[i], new_model,
            #                    split=split_names[i - 1])
            #         perf[i].append(loggers[i].write_epoch(total_epoch))
            total_epoch += 1

        logging.info(f'Fine-tuning the head for {cfg.optim.head_finetune}.')
        for cur_epoch in range(start_epoch, cfg.optim.head_finetune):
            logging.info(f'Epoch {cur_epoch}/{cfg.optim.head_finetune}')
            head_epoch(copy.deepcopy(loggers[0]), loaders[0], new_model, optimizer, scheduler, cfg.optim.batch_accumulation, cfg.sem.L, cfg.sem.V, cfg.sem.tau)

        model = copy.deepcopy(new_model)
        optimizer = create_optimizer(model.parameters(),
                                     new_optimizer_config(cfg))
        scheduler = create_scheduler(optimizer, new_scheduler_config(cfg))
        start_epoch = 0
        logging.info(f'Done distilling at IL iteration {cur_iteration}')


    for logger in loggers:
        logger.close()
    if cfg.train.ckpt_clean:
        clean_ckpt()
    # close wandb
    if cfg.wandb.use:
        run.finish()
        run = None

    logging.info('Task done, results saved in %s', cfg.run_dir)


@register_train('inference-only')
def inference_only(loggers, loaders, model, optimizer=None, scheduler=None):
    """
    Customized pipeline to run inference only.

    Args:
        loggers: List of loggers
        loaders: List of loaders
        model: GNN model
        optimizer: Unused, exists just for API compatibility
        scheduler: Unused, exists just for API compatibility
    """
    num_splits = len(loggers)
    split_names = ['train', 'val', 'test']
    perf = [[] for _ in range(num_splits)]
    cur_epoch = 0
    start_time = time.perf_counter()

    for i in range(0, num_splits):
        eval_epoch(loggers[i], loaders[i], model,
                   split=split_names[i])
        perf[i].append(loggers[i].write_epoch(cur_epoch))

    best_epoch = 0
    best_train = best_val = best_test = ""
    if cfg.metric_best != 'auto':
        # Select again based on val perf of `cfg.metric_best`.
        m = cfg.metric_best
        if m in perf[0][best_epoch]:
            best_train = f"train_{m}: {perf[0][best_epoch][m]:.4f}"
        else:
            # Note: For some datasets it is too expensive to compute
            # the main metric on the training set.
            best_train = f"train_{m}: {0:.4f}"
        best_val = f"val_{m}: {perf[1][best_epoch][m]:.4f}"
        best_test = f"test_{m}: {perf[2][best_epoch][m]:.4f}"

    logging.info(
        f"> Inference | "
        f"train_loss: {perf[0][best_epoch]['loss']:.4f} {best_train}\t"
        f"val_loss: {perf[1][best_epoch]['loss']:.4f} {best_val}\t"
        f"test_loss: {perf[2][best_epoch]['loss']:.4f} {best_test}"
    )
    logging.info(f'Done! took: {time.perf_counter() - start_time:.2f}s')
    for logger in loggers:
        logger.close()


@register_train('PCQM4Mv2-inference')
def ogblsc_inference(loggers, loaders, model, optimizer=None, scheduler=None):
    """
    Customized pipeline to run inference on OGB-LSC PCQM4Mv2.

    Args:
        loggers: Unused, exists just for API compatibility
        loaders: List of loaders
        model: GNN model
        optimizer: Unused, exists just for API compatibility
        scheduler: Unused, exists just for API compatibility
    """
    from ogb.lsc import PCQM4Mv2Evaluator
    evaluator = PCQM4Mv2Evaluator()

    num_splits = 3
    split_names = ['valid', 'test-dev', 'test-challenge']
    assert len(loaders) == num_splits, "Expecting 3 particular splits."

    # Check PCQM4Mv2 prediction targets.
    logging.info(f"0 ({split_names[0]}): {len(loaders[0].dataset)}")
    assert(all([not torch.isnan(d.y)[0] for d in loaders[0].dataset]))
    logging.info(f"1 ({split_names[1]}): {len(loaders[1].dataset)}")
    assert(all([torch.isnan(d.y)[0] for d in loaders[1].dataset]))
    logging.info(f"2 ({split_names[2]}): {len(loaders[2].dataset)}")
    assert(all([torch.isnan(d.y)[0] for d in loaders[2].dataset]))

    model.eval()
    for i in range(num_splits):
        all_true = []
        all_pred = []
        for batch in loaders[i]:
            batch.to(torch.device(cfg.device))
            pred, true = model(batch)
            all_true.append(true.detach().to('cpu', non_blocking=True))
            all_pred.append(pred.detach().to('cpu', non_blocking=True))
        all_true, all_pred = torch.cat(all_true), torch.cat(all_pred)

        if i == 0:
            input_dict = {'y_pred': all_pred.squeeze(),
                          'y_true': all_true.squeeze()}
            result_dict = evaluator.eval(input_dict)
            logging.info(f"{split_names[i]}: MAE = {result_dict['mae']}")  # Get MAE.
        else:
            input_dict = {'y_pred': all_pred.squeeze()}
            evaluator.save_test_submission(input_dict=input_dict,
                                           dir_path=cfg.run_dir,
                                           mode=split_names[i])


@ register_train('log-attn-weights')
def log_attn_weights(loggers, loaders, model, optimizer=None, scheduler=None):
    """
    Customized pipeline to inference on the test set and log the attention
    weights in Transformer modules.

    Args:
        loggers: Unused, exists just for API compatibility
        loaders: List of loaders
        model (torch.nn.Module): GNN model
        optimizer: Unused, exists just for API compatibility
        scheduler: Unused, exists just for API compatibility
    """
    import os.path as osp
    from torch_geometric.loader.dataloader import DataLoader
    from graphgps.utils import unbatch, unbatch_edge_index

    start_time = time.perf_counter()

    # The last loader is a test set.
    l = loaders[-1]
    # To get a random sample, create a new loader that shuffles the test set.
    loader = DataLoader(l.dataset, batch_size=l.batch_size,
                        shuffle=True, num_workers=0)

    output = []
    # batch = next(iter(loader))  # Run one random batch.
    for b_index, batch in enumerate(loader):
        bsize = batch.batch.max().item() + 1  # Batch size.
        if len(output) >= 128:
            break
        print(f">> Batch {b_index}:")

        X_orig = unbatch(batch.x.cpu(), batch.batch.cpu())
        batch.to(torch.device(cfg.device))
        model.eval()
        model(batch)

        # Unbatch to individual graphs.
        X = unbatch(batch.x.cpu(), batch.batch.cpu())
        edge_indices = unbatch_edge_index(batch.edge_index.cpu(),
                                          batch.batch.cpu())
        graphs = []
        for i in range(bsize):
            graphs.append({'num_nodes': len(X[i]),
                           'x_orig': X_orig[i],
                           'x_final': X[i],
                           'edge_index': edge_indices[i],
                           'attn_weights': []  # List with attn weights in layers from 0 to L-1.
                           })

        # Iterate through GPS layers and pull out stored attn weights.
        for l_i, (name, module) in enumerate(model.layers.named_children()):
            if hasattr(module, 'attn_weights'):
                print(l_i, name, module.attn_weights.shape)
                for g_i in range(bsize):
                    # Clip to the number of nodes in this graph.
                    # num_nodes = graphs[g_i]['num_nodes']
                    # aw = module.attn_weights[g_i, :num_nodes, :num_nodes]
                    aw = module.attn_weights[g_i]
                    graphs[g_i]['attn_weights'].append(aw.cpu())
        output += graphs

    logging.info(
        f"[*] Collected a total of {len(output)} graphs and their "
        f"attention weights for {len(output[0]['attn_weights'])} layers.")

    # Save the graphs and their attention stats.
    save_file = osp.join(cfg.run_dir, 'graph_attn_stats.pt')
    logging.info(f"Saving to file: {save_file}")
    torch.save(output, save_file)

    logging.info(f'Done! took: {time.perf_counter() - start_time:.2f}s')
