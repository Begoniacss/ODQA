import os
import shutil
import torch
import sys
import logging
import torch.distributed as dist

logger = logging.getLogger(__name__)


def copy_dir(source, target):
    if os.path.exists(target):
        assert os.path.isdir(target)
        shutil.rmtree(target)

    shutil.copytree(source, target)


def save(model, optimizer, scheduler, step, best_dev_em, opt, dir_path, name):
    path = os.path.join(dir_path, "checkpoint")
    epoch_path = os.path.join(path, name)  # "step-%s" % step)
    os.makedirs(epoch_path, exist_ok=True)
    model.save_pretrained(epoch_path)

    # Save optimizer states
    fp = os.path.join(epoch_path, "optimizer.pth.tar")
    checkpoint = {
        "step": step,
        "optimizer": optimizer.state_dict(),
        "scheduler": scheduler.state_dict(),
        "opt": opt,
        "best_dev_em": best_dev_em,
    }
    torch.save(checkpoint, fp)

    latest_path = os.path.join(path, "latest")
    copy_dir(epoch_path, latest_path)


def restore_epoch(model_class, dir_path, opt, name, reset_params=False):
    epoch_path = os.path.join(dir_path, "checkpoint", name)  # str(epoch))
    epoch_path = os.path.realpath(epoch_path)
    optimizer_path = os.path.join(epoch_path, "optimizer.pth.tar")
    logger.info("Loading %s" % epoch_path)
    model = model_class.from_pretrained(epoch_path)  # , map_location="cuda:"+str(opt.local_rank))
    logger.info("loading checkpoint %s" % optimizer_path)

    local_rank = 0 if opt.local_rank == -1 else opt.local_rank
    checkpoint = torch.load(optimizer_path, map_location="cuda:" + str(local_rank))
    opt_checkpoint = checkpoint["opt"]
    step = checkpoint["step"]
    best_dev_em = checkpoint["best_dev_em"]
    if not reset_params:
        optimizer, scheduler = set_optim(opt_checkpoint, model)
        scheduler.load_state_dict(checkpoint["scheduler"])
        optimizer.load_state_dict(checkpoint["optimizer"])
    else:
        optimizer, scheduler = set_optim(opt, model)

    model = model.to(local_rank)
    return model, optimizer, scheduler, opt_checkpoint, step, best_dev_em


def load_model(model_class, model_path, opt):
    logger.info("Loading %s" % model_path)
    model = model_class.from_pretrained(model_path)  # , map_location="cuda:"+str(opt.local_rank))

    local_rank = 0 if opt.local_rank == -1 else opt.local_rank
    model = model.to(local_rank)
    optimizer, scheduler = set_optim(opt, model)

    return model, optimizer, scheduler


############ OPTIM


class WarmupLinearScheduler(torch.optim.lr_scheduler.LambdaLR):
    def __init__(
        self, optimizer, warmup_steps, t_total, min_ratio, fixed_lr, last_epoch=-1
    ):
        self.warmup_steps = warmup_steps
        self.t_total = t_total
        self.min_ratio = min_ratio
        self.fixed_lr = fixed_lr
        super(WarmupLinearScheduler, self).__init__(
            optimizer, self.lr_lambda, last_epoch=last_epoch
        )

    def lr_lambda(self, step):
        if step < self.warmup_steps:
            return (1 - self.min_ratio) * float(step) / float(
                max(1, self.warmup_steps)
            ) + self.min_ratio
        return 1.0

        if self.fixed_lr:
            return 1.0

        return max(
            0.0,
            1.0
            + float((self.min_ratio - 1) * (step - self.warmup_steps))
            / float(max(1.0, self.t_total - self.warmup_steps)),
        )


class FixedScheduler(torch.optim.lr_scheduler.LambdaLR):
    def __init__(self, optimizer, last_epoch=-1):
        super(FixedScheduler, self).__init__(optimizer, self.lr_lambda, last_epoch=last_epoch)

    def lr_lambda(self, step):
        return 1.0


def clip_gradients(model, clip):
    for p in list(filter(lambda p: p.grad is not None, model.parameters())):
        clip_coef = clip / (p.grad.data.norm(2) + 1e-6)
        if clip_coef < 1:
            p.grad.data.mul_(clip_coef)


# def set_optim(opt, model):
#    #cache_p, model_p = [], []
#    #for n, p in model.named_parameters():
#    #    if 'cache' not in n:
#    #        model_p.append(p)
#    #    else:
#    #        cache_p.append(p)
#    if opt.optim == "adam":
#        optimizer = torch.optim.Adam(
#            model.parameters(), lr=opt.lr, betas=(opt.beta1, opt.beta2), eps=opt.eps
#        )
#    elif opt.optim == "adagrad":
#        optimizer = torch.optim.Adagrad(model.parameters(), lr=opt.lr)
#    elif opt.optim == "adafactor":
#        optimizer = fairseq.optim.adafactor.Adafactor(model.parameters(), lr=opt.lr, relative_step=False)
#    elif opt.optim == "sgd":
#        optimizer = torch.optim.SGD(model.parameters(), lr=opt.lr)
#    if opt.scheduler == 'linear':
#        scheduler = WarmupLinearScheduler(optimizer, opt.warmup, t_total=opt.t_total, min_ratio=opt.min_lr/opt.lr, fixed_lr=opt.fixed_lr)
#    elif opt.scheduler == 'fixed':
#        scheduler = FixedScheduler(optimizer)
#    return optimizer, scheduler


def set_optim(opt, model):
    optimizer = torch.optim.Adam(model.parameters(), lr=opt.lr)
    scheduler = FixedScheduler(optimizer)
    return optimizer, scheduler


def _get_grad_requiring_params(model):
    nb_parameters = 0
    grad_requiring_params = []
    for param in model.parameters():
        if param.requires_grad:
            nb_parameters += param.numel()
            grad_requiring_params.append(param)
    return grad_requiring_params


def print_parameters(net, log_dir, verbose=False):
    file_name = os.path.join(log_dir, "opt.txt")
    num_params = 0
    for param in net.parameters():
        num_params += param.numel()
    message = "[Network] Total number of parameters : %.6f M" % (num_params / 1e6)
    print(message)
    if verbose:
        print(net)
    sys.stdout.flush()
    with open(file_name, "a") as log_file:
        log_file.write(message + "\n")
        with open(file_name, "a") as log_file:
            log_file.write(str(net) + "\n")


def average_master(x, opt):
    if opt.world_size > 1:
        dist.reduce(x, 0, op=dist.ReduceOp.SUM)
        if opt.is_master:
            x = x / opt.world_size
    return x


def sum_master(x, opt):
    if opt.world_size > 1:
        dist.reduce(x, 0, op=dist.ReduceOp.SUM)
    return x


def weighted_average(x, count, opt):
    local_rank = 0 if opt.local_rank == -1 else opt.local_rank
    t_loss = torch.tensor([x * count], device="cuda:" + str(local_rank))
    t_total = torch.tensor([count], device="cuda:" + str(local_rank))
    t_loss = sum_master(t_loss, opt)
    t_total = sum_master(t_total, opt)
    return (t_loss / t_total).item(), t_total.item()


def write_output(glob_path, output_path):
    files = list(glob_path.glob('*.txt'))
    files.sort()
    with open(output_path, 'w') as outfile:
        for path in files:
            with open(path, 'r') as f:
                lines = f.readlines()
                for line in lines:
                    outfile.write(line)
            path.unlink()
    glob_path.rmdir()
