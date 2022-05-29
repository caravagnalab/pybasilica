import torch
import pyro
from pyro.infer import SVI, Trace_ELBO
from pyro.optim import Adam
import pyro.distributions as dist
import torch.nn.functional as F

from pybasilica import utilities
#import utilities


class Error(Exception):
    pass

class NoSignatureToInfer(Error):
    pass


class PyBasilica():

    def __init__(self, x, k_denovo, lr, n_steps, groups=None, beta_fixed=None):
        self._set_data_catalogue(x)
        self._set_beta_fixed(beta_fixed)
        self.k_denovo = k_denovo
        self.lr = lr
        self.n_steps = n_steps
        self._set_groups(groups)
        self._check_args()

    def _set_data_catalogue(self, x):
        try:
            self.x = torch.tensor(x.values).float()
            self.n_samples = x.shape[0]
        except:
            raise TypeError("Invalid count matrix, expected Pandas Dataframe")
        
    def _set_beta_fixed(self, beta_fixed):
        try:
            self.beta_fixed = torch.tensor(beta_fixed.values).float()
            self.k_fixed = beta_fixed.shape[0]
        except:
            self.beta_fixed = None
            self.k_fixed = 0

    def _set_groups(self, groups):
        if groups==None:
            self.groups = groups
        else:
            if isinstance(groups, list) and len(groups)==self.n_samples:
                self.groups = groups
            else:
                raise Exception("invalid groups argument!")

    def _check_args(self):
        if self.k_denovo==0 and self.k_fixed==0:
            raise NoSignatureToInfer("k_denovo and K_fixed could not be zero at the same time!")
    


    def model(self):

        n_samples = self.n_samples
        k_fixed = self.k_fixed
        k_denovo = self.k_denovo
        groups = self.groups

        #----- priors initialization --------------------------------------------------OK
        alpha_mean = dist.Normal(torch.zeros(n_samples, k_denovo + k_fixed), 1).sample()
        
        if groups != None:
            #num_groups = max(params["groups"]) + 1
            n_groups = len(set(groups))
            alpha_tissues = dist.Normal(torch.zeros(n_groups, k_fixed + k_denovo), 1).sample()

            # sample from the alpha prior
            with pyro.plate("k", k_fixed + k_denovo):   # columns
                with pyro.plate("n", n_samples):        # rows
                    alpha = pyro.sample("latent_exposure", dist.Normal(alpha_tissues[groups, :], 1))
        else:
            # sample from the alpha prior
            with pyro.plate("k", k_fixed + k_denovo):   # columns
                with pyro.plate("n", n_samples):        # rows
                    alpha = pyro.sample("latent_exposure", dist.Normal(alpha_mean, 1))
        alpha = torch.exp(alpha)                                    # enforce non negativity
        self.alpha = alpha / (torch.sum(alpha, 1).unsqueeze(-1))    # normalize


        beta_mean = dist.Normal(torch.zeros(k_denovo, 96), 1).sample()

        if k_denovo==0:
            self.beta_denovo = None
            self.beta = self.beta_fixed
        else:
            with pyro.plate("contexts", 96):            # columns
                with pyro.plate("k_denovo", k_denovo):  # rows
                    beta_denovo = pyro.sample("latent_signatures", dist.Normal(beta_mean, 1))
            beta_denovo = torch.exp(beta_denovo)                                        # enforce non negativity
            self.beta_denovo = beta_denovo / (torch.sum(beta_denovo, 1).unsqueeze(-1))  # normalize

            if k_fixed==0:
                self.beta = self.beta_denovo
            else:
                self.beta = torch.cat((self.beta_fixed, self.beta_denovo), axis=0)


        # compute the custom likelihood
        with pyro.plate("mle_contexts", 96):
            with pyro.plate("mle_n", n_samples):
                pyro.factor("obs", self.custom_likelihood())

    

    def guide(self):

        n_samples = self.n_samples
        k_fixed = self.k_fixed
        k_denovo = self.k_denovo
        groups = self.groups

        #----- variational parameters initialization ----------------------------------------OK
        alpha_mean = dist.Normal(torch.zeros(n_samples, k_denovo + k_fixed), 1).sample()
        beta_mean = dist.Normal(torch.zeros(k_denovo, 96), 1).sample()

        with pyro.plate("k", k_fixed + k_denovo):
            with pyro.plate("n", n_samples):
                alpha = pyro.param("alpha", alpha_mean)
                pyro.sample("latent_exposure", dist.Delta(alpha))

        if k_denovo != 0:
            with pyro.plate("contexts", 96):
                with pyro.plate("k_denovo", k_denovo):
                    beta = pyro.param("beta_denovo", beta_mean)
                    pyro.sample("latent_signatures", dist.Delta(beta))


    def inference(self):
        
        pyro.clear_param_store()  # always clear the store before the inference

        # learning global parameters

        adam_params = {"lr": self.lr}
        optimizer = Adam(adam_params)
        elbo = Trace_ELBO()

        self.svi = SVI(self.model, self.guide, optimizer, loss=elbo)

        # inference - do gradient steps
        steps = int(self.n_steps)
        for step in range(steps):
            loss = self.svi.step()

        alpha = pyro.param("alpha").clone().detach()
        alpha = torch.exp(alpha)
        self.alpha = alpha / (torch.sum(alpha, 1).unsqueeze(-1))

        if self.k_denovo == 0:
            self.beta_denovo = None
            self.beta = self.beta_fixed
        else:
            beta_denovo = pyro.param("beta_denovo").clone().detach()
            beta_denovo = torch.exp(beta_denovo)
            self.beta_denovo = beta_denovo / (torch.sum(beta_denovo, 1).unsqueeze(-1))
            if self.k_fixed == 0:
                self.beta = self.beta_denovo
            else:
                self.beta = torch.cat((self.beta_fixed, self.beta_denovo), axis=0)

        self.compute_bic()
    

    def compute_bic(self):
        M = self.x
        alpha = self.alpha
        beta = self.beta
        k_denovo = self.k_denovo

        theta = torch.sum(M, axis=1)

        log_L_Matrix = dist.Poisson(
            torch.matmul(
                torch.matmul(torch.diag(theta), alpha), 
                beta)
                ).log_prob(M)
        log_L = torch.sum(log_L_Matrix)
        log_L = float("{:.3f}".format(log_L.item()))

        k = (alpha.shape[0] * (alpha.shape[1])) + (k_denovo * M.shape[1])
        n = M.shape[0] * M.shape[1]
        bic = k * torch.log(torch.tensor(n)) - (2 * log_L)
        self.bic = bic.item()
    

    # note: just check the order of kl-divergence arguments and why the value is negative
    def regularizer(self):
        if self.beta_denovo == None or self.beta_fixed == None:
            return 0
        else:
            loss = 0
            for fixed in self.beta_fixed:
                for denovo in self.beta_denovo:
                    loss += F.kl_div(fixed, denovo, reduction="batchmean").item()
            return loss
    

    def custom_likelihood(self):
        regularization = self.regularizer()
        likelihood =  dist.Poisson(torch.matmul(torch.matmul(torch.diag(torch.sum(self.x, axis=1)), self.alpha), self.beta)).log_prob(self.x)
        return likelihood + regularization


