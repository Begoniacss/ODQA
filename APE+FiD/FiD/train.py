import os
import time
import sys
import torch
import transformers
# import slurm
import logging
import util
import numpy as np
import torch.distributed as dist
from torch.utils.tensorboard import SummaryWriter
from options import Options
from torch.utils.data import DataLoader, RandomSampler, DistributedSampler, SequentialSampler
from fidt5 import FiDT5
import evaluation
import data
from tqdm.auto import tqdm

logging.getLogger('transformers.tokenization_utils').setLevel(logging.ERROR)
logging.getLogger('transformers.tokenization_utils_base').setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


def train_evaluate(model, optimizer, scheduler, global_step,
                   train_dataset, dev_dataset, opt, collator_function, best_dev_em):
    if opt.is_master:
        tb_logger = SummaryWriter(os.path.join(opt.checkpoint_dir, opt.name))

    train_sampler = (RandomSampler(train_dataset) if opt.local_rank == -1 or opt.world_size == 1
                     else DistributedSampler(train_dataset))
    dev_sampler = SequentialSampler(dev_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler,
                                  batch_size=opt.per_gpu_batch_size, drop_last=True, num_workers=20,
                                  collate_fn=collator_function)
    dev_dataloader = DataLoader(dev_dataset, sampler=dev_sampler, batch_size=opt.per_gpu_batch_size,
                                drop_last=True, num_workers=20, collate_fn=collator_function)

    loss, curr_loss = 0.0, 0.0
    epoch = 1
    model.train()
    while global_step < opt.total_step:
        if opt.world_size > 1:
            train_sampler.set_epoch(epoch)
        epoch += 1
        for i, batch in tqdm(enumerate(train_dataloader)):
            global_step += 1
            idx, answer_ids, answer_mask, context_ids, context_mask = batch
            answer_ids, answer_mask = answer_ids.cuda(), answer_mask.bool().cuda()
            labels = answer_ids.masked_fill(~answer_mask, -100)
            if hasattr(model, "module"):
                model.module.encoder.n_passages = context_ids.size(1)
            else:
                model.encoder.n_passages = context_ids.size(1)
            context_ids = context_ids.cuda().view(context_ids.size(0), -1)
            context_mask = context_mask.cuda().view(context_ids.size(0), -1)
            decoder_input_ids = None

            model.zero_grad()
            inputs = {
                'input_ids': context_ids,
                'attention_mask': context_mask,
                'decoder_attention_mask': answer_mask,
                'decoder_input_ids': decoder_input_ids,
                'labels': labels,
            }
            train_loss = model(**inputs)[0]
            train_loss.backward()
            util.clip_gradients(model, opt.clip)
            optimizer.step()

            scheduler.step()

            train_loss = util.average_master(train_loss, opt)
            curr_loss += train_loss.item()

            if global_step % opt.eval_freq == 0:
                dev_em = evaluate(model, dev_dataset, dev_dataloader, tokenizer, opt)
                if opt.is_master:
                    tb_logger.add_scalar("Evaluation", dev_em, global_step)
                if dev_em > best_dev_em:
                    best_dev_em = dev_em
                    if opt.is_master:
                        model_to_save = model.module if hasattr(model, "module") else model
                        util.save(model_to_save, optimizer, scheduler, global_step, best_dev_em, opt, dir_path,
                                  'best_dev')
                model.train()
            if opt.is_master and global_step % opt.eval_freq == 0:
                logger.info(
                    f"{global_step} / {opt.total_step} -- train = {curr_loss / opt.eval_freq:.3f} | evaluation = {100 * dev_em:.2f}EM | lr = {scheduler.get_last_lr()[0]:.5f}"
                )
                tb_logger.add_scalar("Training", curr_loss / (opt.eval_freq), global_step)
                curr_loss = 0

            if opt.is_master and global_step % (50 * opt.eval_freq) == 0:
                model_to_save = model.module if hasattr(model, "module") else model
                util.save(model_to_save, optimizer, scheduler, global_step, best_dev_em, opt, dir_path,
                          f"step-{global_step}")
            if global_step > opt.total_step:
                break


def evaluate(model, dataset, dataloader, tokenizer, opt):
    model.eval()
    if hasattr(model, "module"):
        model = model.module
    total = 0
    ems = []
    with torch.no_grad():
        for i, batch in enumerate(dataloader):
            idx, answer_ids, answer_mask, context_ids, context_mask = batch
            if hasattr(model, "module"):
                model.module.encoder.n_passages = context_ids.size(1)
            else:
                model.encoder.n_passages = context_ids.size(1)
            context_ids = context_ids.cuda().view(context_ids.size(0), -1)
            context_mask = context_mask.cuda().view(context_mask.size(0), -1)

            outputs = model.generate(
                input_ids=context_ids,
                attention_mask=context_mask,
                max_length=50,
            )

            for k, o in enumerate(outputs):
                ans = tokenizer.decode(o, skip_special_tokens=True)
                gold = dataset.get_example(idx[k]).answers
                ems_score = evaluation.ems(ans, gold)
                total += 1
                ems.append(ems_score)
            if opt.is_master and (i + 1) % opt.eval_print_freq == 0:
                logger.info(f"{i + 1} / {len(dataloader)} -- average = {100 * np.mean(ems):.2f}EM")

    score, total = util.weighted_average(np.mean(ems), total, opt)
    return score


if __name__ == "__main__":
    options = Options()
    opt = options.parse()
    torch.manual_seed(opt.seed)
    # slurm.init_distributed_mode(opt)
    # slurm.init_signal_handler()
    opt.train_batch_size = opt.per_gpu_batch_size * max(1, opt.world_size)
    logger.info("Distributed training")

    logging.getLogger("transformers.tokenization_utils").setLevel(logging.ERROR)

    dir_path = os.path.join(opt.checkpoint_dir, opt.name)

    model_name = 't5-' + opt.model_size
    model_class = FiDT5
    tokenizer = transformers.T5Tokenizer.from_pretrained(model_name)

    collator_function = data.Collator(opt, tokenizer)

    train_examples = data.load_data(opt.train_data_path, n_context=opt.n_context)
    train_dataset = data.Dataset(train_examples, opt.n_context, tokenizer, opt.max_passage_length, opt.no_title)
    dev_examples = data.load_data(opt.dev_data_path, global_rank=opt.global_rank,
                                  # use the global rank and world size attibutes to split the dev set on multiple gpus
                                  world_size=opt.world_size,
                                  n_context=opt.n_context)
    dev_dataset = data.Dataset(dev_examples, opt.n_context, tokenizer, opt.max_passage_length, opt.no_title)

    directory_exists = os.path.exists(dir_path)
    if opt.world_size > 1 and not opt.local_rank == -1:
        torch.distributed.barrier()
    os.makedirs(dir_path, exist_ok=True)
    if not directory_exists and opt.is_master:
        options.print_options(opt)
    if opt.world_size > 1 and not opt.local_rank == -1:
        torch.distributed.barrier()
    file_handler = logging.FileHandler(filename=os.path.join(dir_path, "run.log"))
    stdout_handler = logging.StreamHandler(sys.stdout)
    handlers = [file_handler, stdout_handler]
    logging.basicConfig(
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if opt.is_master else logging.WARN,
        format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
        handlers=handlers,
    )

    if opt.world_size > 1 and not opt.local_rank == -1:
        torch.distributed.barrier()

    global_step = 0
    best_dev_em = 0.

    if not directory_exists and opt.model_path == "none":
        model = model_class.from_pretrained(model_name)
        model = model.to(0 if opt.local_rank == -1 else opt.local_rank)
        optimizer, scheduler = util.set_optim(opt, model)
    elif opt.model_path == "none":
        model, optimizer, scheduler, opt_checkpoint, global_step, best_dev_em = util.restore_epoch(
            model_class, dir_path, opt, reset_params=False, name="latest",
        )
        logger.info("Model loaded from %s" % dir_path)
    else:
        model, optimizer, scheduler, opt_checkpoint, global_step, best_dev_em = util.restore_epoch(
            model_class, opt.model_path, opt, reset_params=True, name="latest",
        )
        logger.info("Model loaded from %s" % opt.model_path)

    if opt.checkpointing_encoder:
        model.encoder.checkpoint = True
    if opt.checkpointing_decoder:
        model.decoder.checkpoint = True
    model.encoder.n_passages = opt.n_context

    if opt.world_size > 1 and opt.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[opt.local_rank],
            output_device=opt.local_rank,
            find_unused_parameters=False,
        )

    logger.info("Start training")
    train_evaluate(model, optimizer, scheduler, global_step,
                   train_dataset, dev_dataset, opt, collator_function, best_dev_em)
