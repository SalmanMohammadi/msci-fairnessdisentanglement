import math
import torch
import torchvision.datasets as dset
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as transforms
import torch.distributions as dist
from itertools import islice
from torch.distributions.normal import Normal
import numpy as np

CUDA = torch.device('cuda')


def compute_gasussian_kl_pair(z_loc_1, z_logvar_1, z_loc_2, z_logvar_2):
    z_var_1 = z_logvar_1.exp()
    z_var_2 = z_logvar_2.exp()
    return z_var_1 / z_var_2 + torch.square(
        z_loc_2 - z_loc_1) / z_var_2 - 1 + z_logvar_2 + z_logvar_1


def gaussian_log_density(x, z_loc, z_logvar):
    norm = torch.tensor(2 * np.pi, device=CUDA, requires_grad=False).log()
    inv_sig = torch.exp(-z_logvar)
    tmp = (x - z_loc)
    return -0.5 * (tmp * tmp * inv_sig + z_logvar + norm)


class ThingsEncoder(nn.Module):
    # input - 350x350(n_channels)
    def __init__(self, z_dim, n_channels):
        super().__init__()
        self.c1 = nn.Conv2d(n_channels, 64, 2, stride=2)
        self.c2 = nn.Conv2d(64, 64, 2, stride=2)
        self.c3 = nn.Conv2d(64, 128, 4, stride=4)
        self.c4 = nn.Conv2d(128, 128, 4, stride=4)
        self.fc5 = nn.Linear(512, 512)
        self.fc_loc = nn.Linear(512, z_dim)
        self.fc_logvar = nn.Linear(512, z_dim)

    def forward(self, x):
        for l in [self.c1, self.c2, self.c3, self.c4]:
            x = nn.functional.relu(l(x))
        x = x.view(-1, 512)
        x = nn.functional.relu(self.fc5(x))
        loc, logvar = self.fc_loc(x), self.fc_logvar(x)
        return loc, logvar


class ThingsDecoder(nn.Module):
    def __init__(self, z_dim, n_channels):
        super().__init__()
        self.fc1 = nn.Linear(z_dim, 512)
        self.fc2 = nn.Linear(512, 1024)
        self.c1 = nn.ConvTranspose2d(64, 128, 4, stride=4)
        self.c2 = nn.ConvTranspose2d(128, 64, 2, stride=2)
        self.c3 = nn.ConvTranspose2d(64, 64, 2, stride=2)
        self.c4 = nn.ConvTranspose2d(64, n_channels, 2, stride=2)

    def forward(self, x):
        x = nn.functional.relu(self.fc1(x))
        x = nn.functional.relu(self.fc2(x))
        x = x.view(-1, 64, 4, 4)
        for l in [self.c1, self.c2, self.c3]:
            x = nn.functional.relu(l(x))
        x = self.c4(x)
        return x


class BaseEncoder(nn.Module):
    # input - 64x64x(n_channels)
    def __init__(self, z_dim, n_channels):
        super().__init__()
        self.c1 = nn.Conv2d(n_channels, 32, 4, stride=2)
        self.c2 = nn.Conv2d(32, 32, 4, stride=2)
        self.c3 = nn.Conv2d(32, 64, 4, stride=2)
        self.c4 = nn.Conv2d(64, 64, 4, stride=2)
        self.fc5 = nn.Linear(256, 256)
        self.fc_loc = nn.Linear(256, z_dim)
        self.fc_logvar = nn.Linear(256, z_dim)

    def forward(self, x):
        for l in [self.c1, self.c2, self.c3, self.c4]:
            x = nn.functional.relu(l(x))
        x = x.view(-1, 256)
        x = nn.functional.relu(self.fc5(x))
        loc, logvar = self.fc_loc(x), self.fc_logvar(x)
        return loc, logvar


class BaseDecoder(nn.Module):
    def __init__(self, z_dim, n_channels):
        super().__init__()
        self.fc1 = nn.Linear(z_dim, 256)
        self.fc2 = nn.Linear(256, 1024)
        self.c1 = nn.ConvTranspose2d(64, 64, 4, padding=1, stride=2)
        self.c2 = nn.ConvTranspose2d(64, 32, 4, padding=1, stride=2)
        self.c3 = nn.ConvTranspose2d(32, 32, 4, padding=1, stride=2)
        self.c4 = nn.ConvTranspose2d(32, n_channels, 4, padding=1, stride=2)

    def forward(self, x):
        x = nn.functional.relu(self.fc1(x))
        x = nn.functional.relu(self.fc2(x))
        x = x.view(-1, 64, 4, 4)
        for l in [self.c1, self.c2, self.c3]:
            x = nn.functional.relu(l(x))
        x = self.c4(x)
        return x


class VAE(nn.Module):
    def __init__(self,
                 z_dim=10,
                 n_channels=3,
                 b=1,
                 use_cuda=True,
                 encoder=BaseEncoder,
                 decoder=BaseDecoder,
                 gamma=None,
                 alpha=None,
                 warm_up=None,
                 use_things=False):
        super().__init__()
        self.encoder = encoder(z_dim, n_channels)
        self.decoder = decoder(z_dim, n_channels)
        self.z_dim = z_dim
        self.b = b
        if use_cuda:
            self.cuda()

    def sample(self, loc, var):
        sig = var.sqrt()
        p_z = torch.randn_like(sig)
        return loc + p_z * sig

    def sample_log(self, loc, logvar):
        sig = torch.exp(0.5 * logvar)
        p_z = torch.randn_like(sig)
        return loc + p_z * sig

    def forward(self, x):
        z_loc, z_logvar = self.encoder(x)
        z = self.sample_log(z_loc, z_logvar)
        x_ = self.decoder(z)
        return x_, z, z_loc, z_logvar

    def batch_forward(self, data, device=CUDA, step=None):
        x = data
        x = x.to(device).squeeze(axis=0)
        x_, z, z_loc, z_logvar = self(x)
        return self.loss(x, x_, z, z_loc, z_logvar)

    def loss(self, x, x_, z, z_loc, z_logvar):
        r_loss = F.binary_cross_entropy_with_logits(
            x_, x, reduction='sum').div(x.shape[0])
        kl = -0.5 * torch.sum(1 + z_logvar - z_loc.pow(2) - z_logvar.exp(),
                              1).mean(0)
        return r_loss + self.b * kl, r_loss, kl

    def reconstruct_img(self, x):
        z_loc, z_logvar = self.encoder(x)
        z = self.sample_log(z_loc, z_logvar)
        loc_img = self.decoder(z)
        return nn.functional.sigmoid(loc_img)

    def batch_representation(self, x):
        z_loc, z_logvar = self.encoder(x)
        return z_loc


class TCVAE(VAE):
    def loss(self, x, x_, z, z_loc, z_logvar):
        r = F.binary_cross_entropy_with_logits(x_, x,
                                               reduction='sum').div(x.shape[0])
        kl = -0.5 * torch.sum(1 + z_logvar - z_loc.pow(2) - z_logvar.exp(),
                              1).mean(0)
        tc = (self.b - 1.) * self.total_correlation(z, z_loc, z_logvar)
        return r + tc + kl, r, kl, tc

    def total_correlation(self, z, z_loc, z_logvar):
        log_qz_prob = gaussian_log_density(z.unsqueeze(1), z_loc.unsqueeze(0),
                                           z_logvar.unsqueeze(0))
        log_qz_prod = torch.logsumexp(log_qz_prob, 1).sum(1)
        log_qz = torch.logsumexp(log_qz_prob.sum(2), 1)

        return torch.mean(log_qz - log_qz_prod)


class AdaGVAE(nn.Module):
    # architecture from Locatello et. al. http://arxiv.org/abs/2002.02886
    def __init__(self,
                 z_dim=10,
                 n_channels=3,
                 b=1,
                 use_cuda=True,
                 adaptive=True,
                 k=None,
                 gamma=1,
                 alpha=1,
                 warm_up=-1,
                 use_things=False):
        super().__init__()
        # create the encoder and decoder networks
        if use_things:
            self.encoder = ThingsEncoder(z_dim, n_channels)
            self.decoder = ThingsDecoder(z_dim, n_channels)
        else:
            self.encoder = BaseEncoder(z_dim, n_channels)
            self.decoder = BaseDecoder(z_dim, n_channels)

        if use_cuda:
            self.cuda()
        self.use_cuda = use_cuda
        self.z_dim = z_dim
        self.b = b
        self.k = k
        self.adaptive = adaptive

    def sample(self, loc, var):
        sig = var.sqrt()
        p_z = torch.randn_like(sig)
        return loc + p_z * sig

    def sample_log(self, loc, logvar):
        sig = torch.exp(0.5 * logvar)
        p_z = torch.randn_like(sig)
        return loc + p_z * sig

    def forward(self, x1, x2):
        z_loc_1, z_logvar_1 = self.encoder(x1)
        z_loc_2, z_logvar_2 = self.encoder(x2)

        z_var_1, z_var_2 = torch.exp(z_logvar_1), torch.exp(z_logvar_2)

        z_loc_1, z_var_1, z_loc_2, z_var_2 = self.average_posterior(
            z_loc_1, z_var_1, z_loc_2, z_var_2)

        z1 = self.sample(z_loc_1, z_var_1)
        z2 = self.sample(z_loc_2, z_var_2)

        x1_ = self.decoder(z1)
        x2_ = self.decoder(z2)

        return x1_, x2_, z_loc_1, z_var_1, z_loc_2, z_var_2

    def batch_forward(self, data, step, device=CUDA):
        x1, x2 = data

        x1 = x1.to(device).squeeze()
        x2 = x2.to(device).squeeze()

        x1_, x2_, z_loc_1, z_var_1, z_loc_2, z_var_2 = self(x1, x2)

        return self.loss(x1, x2, x1_, x2_, z_loc_1, z_var_1, z_loc_2, z_var_2)

    # p(z1, z2 | x1, x2) = p(z1 | x1)p(z2 | x2)
    # averages aggregate posterior according to GVAE strategy in
    # https://www.ijcai.org/Proceedings/2019/0348.pdf
    def average_posterior(self, z_loc_1, z_var_1, z_loc_2, z_var_2):
        dim_kl = compute_gasussian_kl_pair(z_loc_1, z_var_1, z_loc_2, z_var_2)
        tau = 0.5 * (torch.max(dim_kl, 1)[0][:, None] +
                     torch.min(dim_kl, 1)[0][:, None])

        z_loc_1 = torch.where(dim_kl < tau, 0.5 * (z_loc_1 + z_loc_2), z_loc_1)
        z_loc_2 = torch.where(dim_kl < tau, 0.5 * (z_loc_1 + z_loc_2), z_loc_2)

        z_var_1 = torch.where(dim_kl < tau, 0.5 * (z_var_1 + z_var_2), z_var_1)
        z_var_2 = torch.where(dim_kl < tau, 0.5 * (z_var_1 + z_var_2), z_var_2)

        return z_loc_1, z_var_1, z_loc_2, z_var_2

    def loss(self, x1, x2, x1_, x2_, z_loc_1, z_var_1, z_loc_2, z_var_2):
        # returns total_loss, recon_1, recon_2, kl_1, kl_2

        r_1 = nn.functional.binary_cross_entropy_with_logits(
            x1_, x1, reduction='sum').div(64)
        r_2 = nn.functional.binary_cross_entropy_with_logits(
            x2_, x2, reduction='sum').div(64)
        kl_1 = -0.5 * torch.sum(1 + z_var_1.log() - z_loc_1.pow(2) - z_var_1,
                                1).mean(0)
        kl_2 = -0.5 * torch.sum(1 + z_var_2.log() - z_loc_2.pow(2) - z_var_2,
                                1).mean(0)
        r_loss = 0.5 * r_1 + 0.5 * r_2
        kl = self.b * (0.5 * kl_1 + 0.5 * kl_2)
        return r_loss + kl, r_loss, kl, r_1, r_2, kl_1, kl_2

    # define a helper function for reconstructing images
    def reconstruct_img(self, x):
        # encode image x
        z_loc, z_logvar = self.encoder(x)
        # sample in latent space
        z = self.sample_log(z_loc, z_logvar)
        # decode the image (note we don't sample in image space)
        loc_img = self.decoder(z)
        return nn.functional.sigmoid(loc_img)

    def batch_representation(self, x):
        z_loc, z_logvar = self.encoder(x)
        return z_loc


# TripletVAE
class TVAE(AdaGVAE):
    def __init__(self,
                 z_dim=10,
                 n_channels=3,
                 use_cuda=True,
                 adaptive=True,
                 k=None,
                 gamma=1,
                 alpha=1,
                 warm_up=-1,
                 b=None,
                 use_things=False,
                 supervised=True):
        super().__init__(z_dim,
                         n_channels,
                         use_cuda,
                         adaptive,
                         k,
                         use_things=use_things)

        self.alpha = alpha
        self.gamma = gamma
        self.cdf = Normal(
            torch.tensor([0.0]).to(CUDA),
            torch.tensor([1.0]).to(CUDA)).cdf
        self.warm_up = warm_up
        self.train_supervised = supervised
        self.cuda()

    def supervised(self):
        self.train_supervised = True

    def unsupervised(self):
        self.train_supervised = False

    def forward(self, data):
        if self.train_supervised:
            x1, x2, x3 = data
            z_loc_1, z_logvar_1 = self.encoder(x1)
            z_loc_2, z_logvar_2 = self.encoder(x2)
            z_loc_3, z_logvar_3 = self.encoder(x3)

            z1 = self.sample_log(z_loc_1, z_logvar_1)
            z2 = self.sample_log(z_loc_2, z_logvar_2)
            z3 = self.sample_log(z_loc_3, z_logvar_3)

            x1_ = self.decoder(z1)
            x2_ = self.decoder(z2)
            x3_ = self.decoder(z3)

            return x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2, z_logvar_2, z_loc_3, z_logvar_3
        else:
            x1 = data
            z_loc_1, z_logvar_1 = self.encoder(x1)
            z1 = self.sample_log(z_loc_1, z_logvar_1)
            x1_ = self.decoder(z1)
            return x1_, z_loc_1, z_logvar_1

    def batch_forward(self, data, device, step):
        if self.train_supervised:
            x1, x2, x3, y = data
            x1 = x1.to(device).squeeze()
            x2 = x2.to(device).squeeze()
            x3 = x3.to(device).squeeze()
            y = y.to(device).squeeze()

            x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2, z_logvar_2, z_loc_3, z_logvar_3 = self(
                (x1, x2, x3))

            return self.supervised_loss(x1, x2, x3, x1_, x2_, x3_, z_loc_1,
                                        z_logvar_1, z_loc_2, z_logvar_2,
                                        z_loc_3, z_logvar_3, y, step)
        else:
            x = data.to(device).squeeze()
            x_, z_loc, z_logvar = self(x)
            return self.unsupervised_loss(x, x_, z_loc, z_logvar)

    def batch_representation(self, x):
        z_loc, z_logvar = self.encoder(x)
        return z_loc

    def triplet_batch_representation(self, x1, x2, x3):
        z_loc_1, z_logvar_1 = self.encoder(x1)
        z_loc_2, z_logvar_2 = self.encoder(x2)
        z_loc_3, z_logvar_3 = self.encoder(x3)

        return z_loc_1, z_loc_2, z_loc_3

    def unsupervised_loss(self, x, x_, z_loc, z_logvar):
        r = F.binary_cross_entropy_with_logits(x_, x,
                                               reduction='sum').div(x.shape[0])
        kl = -0.5 * torch.sum(1 + z_logvar - z_loc.pow(2) - z_logvar.exp(),
                              1).mean(0)

        return r + kl, r, kl

    def supervised_loss(self, x1, x2, x3, x1_, x2_, x3_, z_loc_1, z_logvar_1,
                        z_loc_2, z_logvar_2, z_loc_3, z_logvar_3, pos, step):

        r_1 = nn.functional.binary_cross_entropy_with_logits(
            x1_, x1, reduction='sum').div(x1.shape[0])
        r_2 = nn.functional.binary_cross_entropy_with_logits(
            x2_, x2, reduction='sum').div(x1.shape[0])
        r_3 = nn.functional.binary_cross_entropy_with_logits(
            x3_, x3, reduction='sum').div(x1.shape[0])

        kl_1 = -0.5 * torch.sum(
            1 + z_logvar_1 - z_loc_1.pow(2) - z_logvar_1.exp(), 1).mean(0)
        kl_2 = -0.5 * torch.sum(
            1 + z_logvar_2 - z_loc_2.pow(2) - z_logvar_2.exp(), 1).mean(0)
        kl_3 = -0.5 * torch.sum(
            1 + z_logvar_3 - z_loc_3.pow(2) - z_logvar_3.exp(), 1).mean(0)

        stacked_loc = torch.stack((z_loc_1, z_loc_2, z_loc_3), dim=-1)
        loc_1_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 0]]
        loc_2_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 1]]
        loc_3_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 2]]
        # d(x1,x2)
        d_1 = torch.norm(loc_1_ - loc_2_, 2, 1, True)
        # d(x1, x3)
        d_2 = torch.norm(loc_1_ - loc_3_, 2, 1, True)
        # d(x2, x3)
        d_3 = torch.norm(loc_2_ - loc_3_, 2, 1, True)

        y = self.cdf((d_2.pow(2) - d_1.pow(2)) * (1 / self.alpha))
        y = y.clamp(min=1e-18).log().sum()

        y_ = self.cdf((d_3.pow(2) - d_1.pow(2)) * (1 / self.alpha))
        y_ = y_.clamp(min=1e-18).log().sum()

        r_loss = (r_1 + r_2 + r_2) / 3
        loss = r_loss + kl_1 + kl_2 + kl_3 - (self.gamma * y) - (self.gamma *
                                                                 y_)

        return loss, r_1, r_2, r_3, kl_1, kl_2, kl_3, y, y_


class KLTVAE(TVAE):
    def loss(self, x1, x2, x3, x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2,
             z_logvar_2, z_loc_3, z_logvar_3, pos, step):
        # returns total_loss, recon_1, recon_2, recon_3, kl_1, kl_2, kl_3
        r_1 = nn.functional.binary_cross_entropy_with_logits(
            x1_, x1, reduction='sum').div(x1.shape[0])
        r_2 = nn.functional.binary_cross_entropy_with_logits(
            x2_, x2, reduction='sum').div(x1.shape[0])
        r_3 = nn.functional.binary_cross_entropy_with_logits(
            x3_, x3, reduction='sum').div(x1.shape[0])

        kl_1 = -0.5 * torch.sum(
            1 + z_logvar_1 - z_loc_1.pow(2) - z_logvar_1.exp(), 1).mean(0)
        kl_2 = -0.5 * torch.sum(
            1 + z_logvar_2 - z_loc_2.pow(2) - z_logvar_2.exp(), 1).mean(0)
        kl_3 = -0.5 * torch.sum(
            1 + z_logvar_3 - z_loc_3.pow(2) - z_logvar_3.exp(), 1).mean(0)

        stacked_loc = torch.stack((z_loc_1, z_loc_2, z_loc_3), dim=-1)
        loc_1_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 0]]
        loc_2_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 1]]
        loc_3_ = stacked_loc[range(z_loc_1.shape[0]), :, pos[:, 2]]

        stacked_var = torch.stack((z_logvar_1, z_logvar_2, z_logvar_3), dim=-1)
        var_1_ = stacked_var[range(z_logvar_1.shape[0]), :, pos[:, 0]]
        var_2_ = stacked_var[range(z_logvar_1.shape[0]), :, pos[:, 1]]
        var_3_ = stacked_var[range(z_logvar_1.shape[0]), :, pos[:, 2]]

        kl_12 = compute_gasussian_kl_pair(loc_1_, var_1_, loc_2_,
                                          var_2_)  #+ 1e-8
        kl_21 = compute_gasussian_kl_pair(loc_2_, var_2_, loc_1_,
                                          var_1_)  #+ 1e-8
        kl_13 = compute_gasussian_kl_pair(loc_1_, var_1_, loc_3_,
                                          var_3_)  #+ 1e-8
        kl_31 = compute_gasussian_kl_pair(loc_3_, var_3_, loc_1_,
                                          var_1_)  #+ 1e-8
        kl_23 = compute_gasussian_kl_pair(loc_2_, var_2_, loc_3_,
                                          var_3_)  #+ 1e-8
        kl_32 = compute_gasussian_kl_pair(loc_3_, var_3_, loc_2_,
                                          var_2_)  #+ 1e-8

        # d(x1,x2)
        d_1 = 0.5 * kl_12 + 0.5 * kl_21  # 0.5 * kl(z1, z2) + 0.5 * kl(z2, z1)
        # d(x1, x3)
        d_2 = 0.5 * kl_13 + 0.5 * kl_31  # 0.5 * kl(z1, z3) + 0.5 * kl(z3, z1)
        # d(x2, x3)
        d_3 = 0.5 * kl_23 + 0.5 * kl_32  # 0.5 * kl(z2, z3) + 0.5 * kl(z3, z2)

        gamma = self.gamma
        y = self.cdf((d_2.pow(2) - d_1.pow(2)) * (1 / self.alpha))
        y = y.clamp(min=1e-18).log().sum()
        y_ = self.cdf((d_3.pow(2) - d_1.pow(2)) * (1 / self.alpha))
        y_ = y_.clamp(min=1e-18).log().sum()
        loss = r_1 + r_2 + r_2 + kl_1 + kl_2 + kl_3 - (gamma * y) - (gamma *
                                                                     y_)
        return loss, r_1, r_2, r_3, kl_1, kl_2, kl_3, y, y_


class AdaTVAE(AdaGVAE):
    def forward(self, x1, x2, x3):
        z_loc_1, z_logvar_1 = self.encoder(x1)
        z_loc_2, z_logvar_2 = self.encoder(x2)
        z_loc_3, z_logvar_3 = self.encoder(x3)

        z_var_1 = torch.exp(z_logvar_1)
        z_var_2 = torch.exp(z_logvar_2)
        z_var_3 = torch.exp(z_logvar_3)

        z_loc_1, z_var_1, z_loc_2, z_var_2 = self.average_posterior(
            z_loc_1, z_var_1, z_loc_2, z_var_2)

        z1 = self.sample(z_loc_1, z_var_1)
        z2 = self.sample(z_loc_2, z_var_2)
        z3 = self.sample(z_loc_3, z_var_3)

        x1_ = self.decoder(z1)
        x2_ = self.decoder(z2)
        x3_ = self.decoder(z3)

        return x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2, z_logvar_2, z_loc_3, z_logvar_3, z1, z2, z3

    def triangle_factorisation(self, z1, z_loc_1, z_logvar_1, z2, z_loc_2,
                               z_logvar_2, z3, z_loc_3, z_logvar_3):
        # select k dimensions with highest dkl(z1|z3)+dkl(z2|z3)
        dim_kl_1 = compute_gasussian_kl_pair(z_loc_1, z_logvar_1.exp(),
                                             z_loc_3, z_logvar_3.exp())
        dim_kl_2 = compute_gasussian_kl_pair(z_loc_2, z_logvar_2.exp(),
                                             z_loc_3, z_logvar_3.exp())
        kl_idx_1 = dim_kl_1.sort(1, descending=True)[1][:, :4]

        z3_ = z3.gather(1, kl_idx_1)
        z_loc_3_ = z_loc_3.gather(1, kl_idx_1)
        z_logvar_3_ = z_logvar_3.gather(1, kl_idx_1)
        # tc term 1 dkl(z1|prod(k)z3)
        tc_1 = self.total_correlation(z1, z_loc_1, z_logvar_1, z3_, z_loc_3_,
                                      z_logvar_3_)
        # tc term 2 dkl(z2|prod(k)z3)
        kl_idx_2 = dim_kl_2.sort(1, descending=True)[1][:, :4]

        z3_1 = z3.gather(1, kl_idx_2)
        z_loc_3_1 = z_loc_3.gather(1, kl_idx_2)
        z_logvar_3_1 = z_logvar_3.gather(1, kl_idx_2)
        # tc term 1 dkl(z1|prod(k)z3)
        tc_2 = self.total_correlation(z2, z_loc_2, z_logvar_2, z3_1, z_loc_3_1,
                                      z_logvar_3_1)

        return tc_1 + tc_2, tc_1, tc_2

    def total_correlation(self, z1, z_loc_1, z_logvar_1, z2, z_loc_2,
                          z_logvar_2):
        # dkl(z1|prod_j z2)
        j_ = z2.shape[1]
        log_qz_1 = gaussian_log_density(z1.view(64, 1, 10), z_loc_1,
                                        z_logvar_1)
        log_qz_2 = gaussian_log_density(z2.view(64, 1, j_), z_loc_2,
                                        z_logvar_2)
        log_qz_prod = torch.logsumexp(log_qz_2, 1).sum(1)
        log_qz = torch.logsumexp(log_qz_1.sum(2), 1)

        return torch.mean(log_qz - log_qz_prod)

    def average_posterior(self, z_loc_1, z_var_1, z_loc_2, z_var_2):
        # average coordinates for q(z|x1) and q(z|x2)
        dim_kl = compute_gasussian_kl_pair(z_loc_1, z_var_1, z_loc_2, z_var_2)
        kl_idx = dim_kl.sort(1)[1][:, :4]
        loc_avg = 0.5 * (z_loc_1.gather(1, kl_idx) + z_loc_2.gather(1, kl_idx))
        var_avg = 0.5 * (z_var_1.gather(1, kl_idx) + z_var_2.gather(1, kl_idx))
        z_loc_1 = z_loc_1.scatter(1, kl_idx, loc_avg)
        z_loc_2 = z_loc_2.scatter(1, kl_idx, loc_avg)

        z_var_1 = z_var_1.scatter(1, kl_idx, var_avg)
        z_var_2 = z_var_2.scatter(1, kl_idx, var_avg)
        return z_loc_1, z_var_1, z_loc_2, z_var_2

    def batch_forward(self, data, device):
        x1, x2, x3 = data

        x1 = x1.to(device).permute(1, 0, 2, 3)
        x2 = x2.to(device).permute(1, 0, 2, 3)
        x3 = x3.to(device).permute(1, 0, 2, 3)

        x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2, z_logvar_2, z_loc_3, z_logvar_3, z1, z2, z3 = self(
            x1, x2, x3)

        return self.loss(x1, x2, x3, x1_, x2_, x3_, z_loc_1, z_logvar_1,
                         z_loc_2, z_logvar_2, z_loc_3, z_logvar_3, z1, z2, z3)

    def loss(self, x1, x2, x3, x1_, x2_, x3_, z_loc_1, z_logvar_1, z_loc_2,
             z_logvar_2, z_loc_3, z_logvar_3, z1, z2, z3):
        # returns total_loss, recon_1, recon_2, recon_3, kl_1, kl_2, kl_3

        r_1 = nn.functional.binary_cross_entropy_with_logits(
            x1_, x1, reduction='sum').div(64)
        r_2 = nn.functional.binary_cross_entropy_with_logits(
            x2_, x2, reduction='sum').div(64)
        r_3 = nn.functional.binary_cross_entropy_with_logits(
            x3_, x3, reduction='sum').div(64)

        kl_1 = -0.5 * torch.sum(
            1 + z_logvar_1 - z_loc_1.pow(2) - z_logvar_1.exp(), 1).mean(0)
        kl_2 = -0.5 * torch.sum(
            1 + z_logvar_2 - z_loc_2.pow(2) - z_logvar_2.exp(), 1).mean(0)
        kl_3 = -0.5 * torch.sum(
            1 + z_logvar_3 - z_loc_2.pow(2) - z_logvar_3.exp(), 1).mean(0)

        tc, tc_1, tc_2 = self.triangle_factorisation(z1, z_loc_1, z_logvar_1,
                                                     z2, z_loc_2, z_logvar_2,
                                                     z3, z_loc_3, z_logvar_3)
        loss = r_1 + r_2 + r_2 + kl_1 + kl_2 + kl_3 - (2 * tc)

        return loss, r_1, r_2, r_3, kl_1, kl_2, kl_3, tc, tc_1, tc_2


# similar to train() but for a given number steps by sampling from an "infinite" dataset
def train_steps(model,
                dataset,
                optimizer,
                num_steps=300000,
                device=CUDA,
                verbose=True,
                writer=None,
                log_interval=100,
                write_interval=6000,
                metrics_labels=None):
    """
    Trains the model for a single 'epoch' on the data
    """
    model.train()
    # train_loss = 0
    metrics_mean = []
    dataset_len = num_steps * dataset.batch_size
    for batch_id, data in enumerate(dataset):
        optimizer.zero_grad()
        loss, (*metrics) = model.batch_forward(data,
                                               step=batch_id,
                                               device=device)
        loss.backward()
        optimizer.step()

        # train_loss += loss.item()
        # metrics_mean.append([x.item() for x in metrics])
        if ((batch_id % log_interval == 0) or
            (batch_id == dataset_len - 1)) and verbose:
            print('Train step: {}, loss: {}'.format(batch_id, loss.item()))
            if metrics_labels:
                print(", ".join(
                    list(
                        map(lambda x: "%s: %.5f" % x,
                            zip(metrics_labels, metrics)))))
            else:
                print(metrics)
        if batch_id % write_interval == 0 and writer:
            writer.add_scalar('train/loss', loss.item(), batch_id)
            if metrics_labels:
                for label, metric in zip(metrics_labels, metrics):
                    writer.add_scalar('train/' + label, metric, batch_id)


def train_things(model,
                 dataset,
                 optimizer,
                 num_steps=300000,
                 device=CUDA,
                 verbose=True,
                 writer=None,
                 log_interval=100,
                 write_interval=6000,
                 metrics_labels=None,
                 prefix=''):
    model.train()
    steps = 0
    epoch = 1
    dataset_len = num_steps
    while steps < num_steps:
        for batch_id, data in enumerate(islice(dataset, num_steps - steps)):
            optimizer.zero_grad()
            loss, (*metrics) = model.batch_forward(data,
                                                   step=batch_id,
                                                   device=device)
            loss.backward()
            optimizer.step()
            # train_loss += loss.item()
            # metrics_mean.append([x.item() for x in metrics])
            if ((steps % log_interval == 0) or
                (steps == dataset_len - 1)) and verbose:
                print('Train step: {}, loss: {}'.format(steps, loss.item()))
                if metrics_labels:
                    print(", ".join(
                        list(
                            map(lambda x: "%s: %.5f" % x,
                                zip(metrics_labels, metrics)))))
                else:
                    print(metrics)
            if steps % write_interval == 0 and writer:
                writer.add_scalar(f"{prefix}_train/loss", loss.item(), steps)
                if metrics_labels:
                    for label, metric in zip(metrics_labels, metrics):
                        writer.add_scalar(f"{prefix}_train/{label}", metric,
                                          steps)
            steps += 1
        epoch += 1
    writer.flush()


def train(model,
          dataset,
          epoch,
          optimizer,
          device=CUDA,
          verbose=True,
          writer=None,
          log_interval=100,
          metrics_labels=None):
    """
    Trains the model for a single 'epoch' on the data
    """
    model.train()
    train_loss = 0
    metrics_mean = []
    dataset_len = len(dataset) * dataset.batch_size
    for batch_id, data in enumerate(dataset):
        optimizer.zero_grad()
        loss, (*metrics) = model.batch_forward(data, device=device)
        loss.backward()
        optimizer.step()

        train_loss += loss.item()
        metrics_mean.append([x.item() for x in metrics])

        data_len = len(data[0])
        if batch_id % log_interval == 0 and verbose:
            print('Train Epoch: {}, batch: {}, loss: {}'.format(
                epoch, batch_id,
                loss.item() / data_len))
            metrics = [x.item() / data_len for x in metrics]
            if metrics_labels:
                print(", ".join(
                    list(
                        map(lambda x: "%s: %.5f" % x,
                            zip(metrics_labels, metrics)))))
            else:
                print(metrics)

    metrics_mean = np.array(metrics_mean)
    metrics_mean = np.sum(metrics_mean, axis=0) / dataset_len

    if writer:
        writer.add_scalar('train/loss', train_loss / dataset_len, epoch)
        for label, metric in zip(metrics_labels, metrics_mean):
            writer.add_scalar('train/' + label, metric, epoch)
    if verbose:
        print('====> Epoch: {} Average loss: {:.4f}'.format(
            epoch, train_loss / dataset_len))


def test(model,
         dataset,
         verbose=True,
         device=CUDA,
         metrics_labels=None,
         writer=None,
         experiment_id=0):
    """
    Evaluates the model
    """
    model.eval()
    test_loss = 0
    metrics_mean = []
    print(len(dataset.dataset))
    with torch.no_grad():
        for batch_id, data in enumerate(dataset):
            loss, (*metrics) = model.batch_forward(data, device=device, step=0)
            metrics_mean.append([x.item() for x in metrics])
            test_loss += loss.item()

    metrics_mean = np.array(metrics_mean)
    test_loss /= len(dataset.dataset)
    metrics_mean = np.sum(metrics_mean, axis=0) / len(dataset.dataset)
    # metrics = [x.item()/len(dataset.dataset) for x in metrics]
    if verbose:
        if metrics_labels:
            print(", ".join(
                list(
                    map(lambda x: "%s: %.5f" % x,
                        zip(metrics_labels, metrics_mean)))))
        print("Eval: ", test_loss)
    if writer:
        writer.add_scalar('test/loss', test_loss, experiment_id)
        for label, metric in zip(metrics_labels, metrics_mean):
            writer.add_scalar('test/' + label, metric, experiment_id)
    return test_loss, metrics_mean


def batch_sample_latents(model, data, num_points, batch_size=16, device=CUDA):
    model.eval()
    latents = torch.zeros((num_points, 10))
    labels = torch.zeros((num_points, 7))
    assert len(data.dataset) >= num_points
    with torch.no_grad():
        for i, (X, Z) in enumerate(islice(data,
                                          (num_points // batch_size) + 1)):
            X = X.reshape(batch_size, 3, 64, 64).to(device)
            Z = Z.reshape(batch_size, -1).to(device)
            n = min(num_points - ((i) * batch_size), batch_size)
            latents[i * batch_size:(i * batch_size) +
                    n, :] = model.batch_representation(X[:n])
            labels[i * batch_size:(i * batch_size) + n, :] = Z[:n]

    return latents, labels


def batch_sample_latent_triplets(model,
                                 data,
                                 num_points,
                                 batch_size=16,
                                 device=CUDA):
    model.eval()
    latents = torch.zeros((num_points, 3, model.z_dim))
    labels = torch.zeros((num_points, 3))
    assert len(data.dataset) >= num_points
    with torch.no_grad():
        for i, (x1, x2, x3,
                y) in enumerate(islice(data, (num_points // batch_size) + 1)):
            x1 = x1.to(device).squeeze()
            x2 = x2.to(device).squeeze()
            x3 = x3.to(device).squeeze()
            y = y.to(device).squeeze()
            n = min(num_points - ((i) * batch_size), batch_size)
            z1 = model.batch_representation(x1[:n])
            z2 = model.batch_representation(x2[:n])
            z3 = model.batch_representation(x3[:n])
            latents[i * batch_size:(i * batch_size) + n, 0] = z1
            latents[i * batch_size:(i * batch_size) + n, 1] = z2
            latents[i * batch_size:(i * batch_size) + n, 2] = z3
            labels[i * batch_size:(i * batch_size) + n, :] = y[:n]

    return latents.view(num_points, -1), labels
