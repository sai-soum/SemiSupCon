import pytorch_lightning as pl
import torch
from torch import nn
import matplotlib.pyplot as plt
import wandb
from torch import optim
from pytorch_lightning.cli import OptimizerCallable
from SemiSupCon.models.semisupcon import SemiSupCon
from torchmetrics.functional import auroc, average_precision
from SemiSupCon.models.utils import confusion_matrix
import numpy as np

class FinetuneSemiSupCon(pl.LightningModule):
    
    def __init__(self, encoder, 
        optimizer: OptimizerCallable = None,
        freeze_encoder = True,
        checkpoint = None,
        mlp_head = True,
        checkpoint_head = None,
        task = 'mtat_top50'):
        super().__init__()
        
        self.task = task
        if self.task == 'mtat_top50':
            self.loss_fn = nn.BCEWithLogitsLoss()
            self.n_classes = 50
            
        self.semisupcon = SemiSupCon(encoder)
        self.optimizer = optimizer
        
        self.freeze_encoder = freeze_encoder
        self.checkpoint = checkpoint
        self.checkpoint_head = checkpoint_head
        
        if self.checkpoint:
            self.load_encoder_weights_from_checkpoint(self.checkpoint)
            
        if self.freeze_encoder:
            self.semisupcon.freeze()
            self.semisupcon.eval()
            
            
        self.agg_preds = []
        self.agg_ground_truth = []
        
        
            
        if mlp_head:
            self.head = nn.Sequential(
                nn.Linear(512, 512, bias=False),
                nn.ReLU(),
                nn.Linear(512, self.n_classes, bias=False),
            )
        else:
            self.head = nn.Linear(512, self.n_classes, bias=False)
            
        
        if self.checkpoint_head:
            self.head.load_state_dict(torch.load(self.checkpoint_head)['state_dict'], strict = False)
            
            
        if self.task == 'mtat_top50':
            self.class_names = ['guitar', 'classical', 'slow', 'techno', 'strings', 'drums',
       'electronic', 'rock', 'fast', 'piano', 'ambient', 'beat', 'violin',
       'vocal', 'synth', 'female', 'indian', 'opera', 'male', 'singing',
       'vocals', 'no vocals', 'harpsichord', 'loud', 'quiet', 'flute', 'woman',
       'male vocal', 'no vocal', 'pop', 'soft', 'sitar', 'solo', 'man',
       'classic', 'choir', 'voice', 'new age', 'dance', 'female vocal',
       'male voice', 'beats', 'harp', 'cello', 'no voice', 'weird', 'country',
       'female voice', 'metal', 'choral']
        else:
            self.class_names = None
            
        
        
    def load_encoder_weights_from_checkpoint(self,checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        self.semisupcon.load_state_dict(checkpoint['state_dict'], strict = False)
        
    def load_head_weights_from_checkpoint(self,checkpoint_path):
        checkpoint = torch.load(checkpoint_path)
        self.head.load_state_dict(checkpoint['state_dict'], strict = False)
        
    def forward(self,x):
        
        if isinstance(x,dict):
            wav = x['audio']
            labels = x['labels'].squeeze(1)
        else:
            wav = x
            labels = torch.zeros(wav.shape[0]*wav.shape[1],10)
        
        
        # x is of shape [B,T]:
        
        encoded = self.semisupcon(wav)['encoded']
        projected = self.head(encoded)
        
        return {
            'projected':projected,
            'labels':labels,
            'encoded':encoded
        }
        
        
    def training_step(self, batch, batch_idx):
            
        x = batch
        out_ = self(x)
        
        logits = out_['projected']
        labels = out_['labels']
        
        loss = self.loss_fn(logits,labels.float())
        
        #get metrics
        preds = torch.sigmoid(logits)
        aurocs = auroc(preds,labels,task = 'multilabel',num_labels = self.n_classes)
        ap_score = average_precision(preds,labels,task = 'multilabel',num_labels = self.n_classes)
        
        self.log('train_loss',loss, on_step = True, on_epoch = True, prog_bar = True, sync_dist = True)
        self.log('train_auroc',aurocs, on_step = True, on_epoch = True, prog_bar = True, sync_dist = True)
        self.log('train_ap',ap_score, on_step = True, on_epoch = True, prog_bar = True, sync_dist = True)
        
        return loss
    
    
    def validation_step(self,batch,batch_idx):
        x = batch
        out_ = self(x)
        
        logits = out_['projected']
        labels = out_['labels']
        
        loss = self.loss_fn(logits,labels.float())
    
        #get metrics
        preds = torch.sigmoid(logits)
        aurocs = auroc(preds,labels,task = 'multilabel',num_labels = self.n_classes)
        ap_score = average_precision(preds,labels,task = 'multilabel',num_labels = self.n_classes)
        
        self.log('val_loss',loss, on_step = False, on_epoch = True, prog_bar = True, sync_dist = True)
        self.log('val_auroc',aurocs, on_step = False, on_epoch = True, prog_bar = True, sync_dist = True)
        self.log('val_ap',ap_score, on_step = False, on_epoch = True, prog_bar = True, sync_dist = True)
        
        return loss
    
    def test_step(self,batch,batch_idx):
        x = batch
        
        x['audio'] = x['audio'].squeeze(0).unsqueeze(1).unsqueeze(1)
        x['labels'] = x['labels'].squeeze(0)
        
        out_ = self(x)
        
        
        logits = out_['projected']
        labels = out_['labels']
        
        logits = logits.mean(0).unsqueeze(0)
        labels = labels[0].unsqueeze(0)
        
        self.agg_ground_truth.append(labels)
        self.agg_preds.append(logits)
        
        loss = self.loss_fn(logits,labels.float())
        return loss
    
    
    def on_test_epoch_end(self):
        preds = torch.cat(self.agg_preds,0)
        ground_truth = torch.cat(self.agg_ground_truth,0)
        
        preds = torch.sigmoid(preds)
        
        loss = self.loss_fn(preds,ground_truth.float())
        aurocs = auroc(preds,ground_truth,task = 'multilabel',num_labels = self.n_classes)
        ap_score = average_precision(preds,ground_truth,task = 'multilabel',num_labels = self.n_classes)
        cmat = confusion_matrix(preds,ground_truth,self.n_classes).cpu().numpy()
        # normalize the cmatrix row-wise
        cmat = cmat.astype('float') / cmat.sum(axis=1)[:, np.newaxis]
        
        self.log('test_loss',loss, on_step = False, on_epoch = True, prog_bar = False, sync_dist = True)
        self.log('test_auroc',aurocs, on_step = False, on_epoch = True, prog_bar = False, sync_dist = True)
        self.log('test_ap',ap_score, on_step = False, on_epoch = True, prog_bar = False, sync_dist = True)
        
        # make a pretty matplotlib heatmap of the confusion matrix
        fig, ax = plt.subplots(figsize=(30,30))
        im = ax.imshow(cmat)
        
        # We want to show all ticks...
        ax.set_xticks(np.arange(len(self.class_names)))
        ax.set_yticks(np.arange(len(self.class_names)))
        # ... and label them with the respective list entries
        ax.set_xticklabels(self.class_names)
        ax.set_yticklabels(self.class_names)
        
        # Rotate the tick labels and set their alignment.
        plt.setp(ax.get_xticklabels(), rotation=45, ha="right",
                    rotation_mode="anchor")
        
        # Loop over data dimensions and create text annotations.
        for i in range(len(self.class_names)):
            
            for j in range(len(self.class_names)):
                # round to 2 decimal places
                leg = round(cmat[i,j],2)  
                text = ax.text(j, i, leg,
                               ha="center", va="center", color="w")
                
        ax.set_title("Confusion matrix")
        fig.tight_layout()
        self.logger.log_image(
            'confusion_matrix', [wandb.Image(fig)])
        
        
        wandb.log({"Confusion Matrix":self.custom_wandb_confusion_matrix(cmat)})
        
        self.agg_preds = []
        self.agg_ground_truth = []
    
    
    def custom_wandb_confusion_matrix(self,confusion_matrix):
        data = []
        for i in range(confusion_matrix.shape[0]):
            for j in range(confusion_matrix.shape[1]):
                data.append([self.class_names[i],self.class_names[j],confusion_matrix[i,j]])
            
        fields = {
            'target': 'target',
            'prediction': 'prediction',
            'value': 'value'
        }
        
        return wandb.plot_table(
            "Confusion matrix",
            wandb.Table(data=data, columns=["target", "prediction", "value"]),
            fields,
            {'title': 'Confusion matrix'}
        )
                
        
    
    def configure_optimizers(self):
        if self.optimizer is None:
            optimizer = optim.Adam(
                self.parameters(), lr=1e-4, betas=(0.9, 0.999), eps=1e-8)
        else:
            optimizer = self.optimizer(self.parameters())
            
        return optimizer
    
    def on_checkpoint_save(self, checkpoint):
        checkpoint['state_dict'] = self.head.state_dict()