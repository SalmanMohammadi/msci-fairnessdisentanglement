import torch
import argparse
import torch.nn as nn
import torch.optim as optim
import torchvision.transforms as transforms
import models.models_pgm 
from models.models_pgm import WReN
from data import rpm_data as rpm
import matplotlib.pyplot as plt
from torch.utils.tensorboard import SummaryWriter 

CUDA = torch.device('cuda')

parser = argparse.ArgumentParser()
# parser.add_argument("--model", type=str)
parser.add_argument("--train", action="store_true")
parser.add_argument("--test", action="store_true")
parser.add_argument("--save", action="store_true")
parser.add_argument("--experiment_name", type=str, default='')
parser.add_argument("--experiment_id", type=int, default=0)
args = parser.parse_args()

if args.train and args.test:
    parser.error("Can't have both --train and --test")

experiment_id = '/' + str(args.experiment_id)
experiment_name = '/' + args.experiment_name if args.experiment_name else ''
model_path = 'tmp/' + experiment_name + experiment_id

labels = ['accuracy']
(_, _), (train_data, test_data) = rpm.get_datasets()

model = WReN().to(CUDA)
writer = SummaryWriter(log_dir=model_path)
if not args.test:
    opt = optim.Adam(model.parameters(), lr=0.0001, betas=(0.9, 0.999), eps=1e-8)
    models.models_pgm.train_steps(model, train_data, opt, verbose=True, writer=writer,
                metrics_labels=labels)
    if args.save:
        torch.save(model.state_dict(), model_path + ".pt")

if not args.train:
    _, metrics = models.models_pgm.test(model, test_data, verbose=True, metrics_labels=labels, 
                            writer=writer, experiment_id=args.experiment_id)

writer.close()

    # metrics_labels = ['hparam/'+x for x in config.model['metrics_labels']]
    # writer.add_hparams(hparam_dict=config.hparams, metric_dict=dict(zip(metrics_labels, metrics)))