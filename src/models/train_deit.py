import os
import torch
from torch.utils.data import DataLoader, random_split
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint, LearningRateMonitor
from pytorch_lightning.loggers import CSVLogger
import datetime
from torchmetrics.classification import (
    MulticlassPrecision, MulticlassRecall, MulticlassF1Score,
    MulticlassAveragePrecision, MulticlassConfusionMatrix
)
import numpy as np
from pytorch_lightning import seed_everything
from transformers import AutoModelForImageClassification
import time
import csv


class DeiTTinyForClassification(pl.LightningModule):
    def __init__(self, num_classes=10, learning_rate=0.001):
        super().__init__()
        self.save_hyperparameters()

        # Save the learning rate as a class attribute
        self.learning_rate = learning_rate

        # Load DeiT-Tiny from Hugging Face
        self.model = AutoModelForImageClassification.from_pretrained(
            "facebook/deit-tiny-patch16-224", 
            num_labels=num_classes,
            ignore_mismatched_sizes=True  # Ignore size mismatch in the classifier layer
        )

        # Cross-entropy loss for classification
        self.criterion = torch.nn.CrossEntropyLoss()

        # Metrics
        self.precision = MulticlassPrecision(num_classes=num_classes, average=None)
        self.recall = MulticlassRecall(num_classes=num_classes, average=None)
        self.f1 = MulticlassF1Score(num_classes=num_classes, average=None)
        self.map = MulticlassAveragePrecision(num_classes=num_classes)
        self.confusion_matrix = MulticlassConfusionMatrix(num_classes=num_classes)

        # Gradient norms per epoch
        self.grad_norm_values = []

    def forward(self, x):
        return self.model(x).logits  # Only return class logits

    def training_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = self.criterion(logits, y)

        # Calculate metrics
        preds = torch.argmax(logits, dim=1)
        precision = self.precision(preds, y)
        recall = self.recall(preds, y)
        f1 = self.f1(preds, y)
        ap = self.map(logits, y)

        # Log metrics
        self.log("train_loss", loss, on_step=False, on_epoch=True)
        self.log("train_precision", precision.mean(), on_step=False, on_epoch=True)
        self.log("train_recall", recall.mean(), on_step=False, on_epoch=True)
        self.log("train_f1", f1.mean(), on_step=False, on_epoch=True)
        self.log("train_map", ap.mean(), on_step=False, on_epoch=True)

        return loss

    def validation_step(self, batch, batch_idx):
        x, y = batch
        logits = self.forward(x)
        loss = self.criterion(logits, y)

        # Calculate metrics
        preds = torch.argmax(logits, dim=1)
        precision = self.precision(preds, y)
        recall = self.recall(preds, y)
        f1 = self.f1(preds, y)
        ap = self.map(logits, y)

        # Ensure AP is iterable and handle NaN values
        if ap.dim() == 0:
            ap = ap.unsqueeze(0)
        ap = torch.nan_to_num(ap, nan=0.0)

        confusion_matrix = self.confusion_matrix(preds, y).cpu().numpy()

        # Derive FP/FN from confusion matrix
        false_positives = confusion_matrix.sum(axis=0) - np.diag(confusion_matrix)
        false_negatives = confusion_matrix.sum(axis=1) - np.diag(confusion_matrix)

        # Log metrics
        self.log("val_loss", loss, on_step=False, on_epoch=True)
        self.log("val_precision", precision.mean(), on_step=False, on_epoch=True)
        self.log("val_recall", recall.mean(), on_step=False, on_epoch=True)
        self.log("val_f1", f1.mean(), on_step=False, on_epoch=True)
        self.log("val_map", ap.mean(), on_step=False, on_epoch=True)

        # Log per-class metrics
        for i, (fp, fn, class_ap) in enumerate(zip(false_positives, false_negatives, ap)):
            self.log(f"class_{i}_fp", fp, on_step=False, on_epoch=True)
            self.log(f"class_{i}_fn", fn, on_step=False, on_epoch=True)
            self.log(f"class_{i}_ap", class_ap, on_step=False, on_epoch=True)

        return loss

    def on_before_backward(self, loss):
        # Track gradient norms
        total_norm = 0.0
        for p in self.parameters():
            if p.grad is not None:
                total_norm += p.grad.norm(2).item() ** 2
        total_norm = total_norm ** 0.5
        self.grad_norm_values.append(total_norm)
        self.log("grad_norm", total_norm, on_step=True, on_epoch=False)

    def on_train_epoch_end(self):
        # Log average gradient norm for the epoch
        if self.grad_norm_values:  # Ensure there are values to average
            avg_grad_norm = np.mean(self.grad_norm_values)
            self.log("avg_grad_norm", avg_grad_norm, on_epoch=True)
            self.grad_norm_values = []  # Clear the list for the next epoch

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate)
        scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=10, gamma=0.1)
        return [optimizer], [scheduler]


# Prepare CIFAR-10 dataset
def prepare_data(data_dir="data/cifar10"):
    transform = transforms.Compose([
        transforms.Resize((224, 224)),  # Resize images to 224x224
        transforms.ToTensor(),
        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
    ])
    dataset = datasets.CIFAR10(root=data_dir, train=True, download=True, transform=transform)
    train_len = int(len(dataset) * 0.8)
    val_len = len(dataset) - train_len
    train_dataset, val_dataset = random_split(dataset, [train_len, val_len])
    return train_dataset, val_dataset


def main():
    saved_models_dir = "saved_models"
    logs_dir = "logs"
    os.makedirs(saved_models_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    # Read training parameters from environment variables, or use default values
    epochs = int(os.getenv("EPOCHS", 10)) 
    learning_rate = float(os.getenv("LEARNING_RATE", 0.001)) 
    batch_size = int(os.getenv("BATCH_SIZE", 32)) 

    # Seed everything for reproducibility
    seed_everything(42, workers=True)

    # Dataset and DataLoader
    train_dataset, val_dataset = prepare_data()
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=4,
        persistent_workers=True
    )
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        num_workers=4,
        persistent_workers=True
    )

    # Initialize model
    model = DeiTTinyForClassification(num_classes=10, learning_rate=learning_rate)

    # Callbacks
    current_time = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    checkpoint_callback = ModelCheckpoint(
        monitor="val_map",
        mode="max",
        dirpath=saved_models_dir,
        filename=f"DeiTTinyForClassification_{current_time}_best",
        save_top_k=1
    )
    checkpoint_callback_epoch = ModelCheckpoint(
        every_n_epochs=1,
        dirpath=saved_models_dir,
        filename=f"DeiTTinyForClassification_{current_time}_epoch{{epoch}}"
    )
    lr_monitor = LearningRateMonitor(logging_interval="epoch")
    csv_logger = CSVLogger(logs_dir, name=f"DeiTTinyForClassification_{current_time}")

    # Create a CSV to store metrics
    csv_metrics = ['epoch', 'train_loss', 'train_precision', 'train_recall', 'train_f1', 'train_map', 
                   'val_loss', 'val_precision', 'val_recall', 'val_f1', 'val_map', 
                   'grad_norm', 'avg_grad_norm', 'learning_rate']
    with open(os.path.join(logs_dir, f"metrics_{current_time}.csv"), mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(csv_metrics)

    # Trainer
    start_time = time.time()
    trainer = pl.Trainer(
        max_epochs=epochs,
        callbacks=[checkpoint_callback, checkpoint_callback_epoch, lr_monitor],
        logger=csv_logger,
        log_every_n_steps=10,
                accelerator="gpu" if torch.cuda.is_available() else "cpu",
        devices=1
    )
    
    # Start training
    trainer.fit(model, train_loader, val_loader)
    training_time = time.time() - start_time

    # Log training time
    csv_logger.log_hyperparams({"training_time": training_time})
    print(f"Training completed in {training_time:.2f} seconds")

    # After training, gather and save metrics to CSV
    with open(os.path.join(logs_dir, f"metrics_{current_time}.csv"), mode='a', newline='') as file:
        writer = csv.writer(file)

        for epoch in range(epochs):
            # Get the logged metrics for each epoch
            train_loss = trainer.callback_metrics.get('train_loss', 0)
            train_precision = trainer.callback_metrics.get('train_precision', 0)
            train_recall = trainer.callback_metrics.get('train_recall', 0)
            train_f1 = trainer.callback_metrics.get('train_f1', 0)
            train_map = trainer.callback_metrics.get('train_map', 0)

            val_loss = trainer.callback_metrics.get('val_loss', 0)
            val_precision = trainer.callback_metrics.get('val_precision', 0)
            val_recall = trainer.callback_metrics.get('val_recall', 0)
            val_f1 = trainer.callback_metrics.get('val_f1', 0)
            val_map = trainer.callback_metrics.get('val_map', 0)

            grad_norm = trainer.callback_metrics.get('grad_norm', 0)
            avg_grad_norm = trainer.callback_metrics.get('avg_grad_norm', 0)
            learning_rate = trainer.callback_metrics.get('lr-Adam', 0)

            # Write the metrics of each epoch to CSV
            writer.writerow([epoch, train_loss, train_precision, train_recall, train_f1, train_map, 
                             val_loss, val_precision, val_recall, val_f1, val_map, 
                             grad_norm, avg_grad_norm, learning_rate])


if __name__ == "__main__":
    main()

