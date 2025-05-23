import argparse
import datetime
import enum
import inspect
import pathlib
import random
import os

import codetiming
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import torch
import torch.nn.functional as F
import torch_geometric.utils as tg_utils
import wandb
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader

from my_graphs_dataset import GraphDataset
from MIDS_dataset import MIDSDataset, MIDSProbabilitiesDataset, MIDSLabelsDataset, inspect_dataset
from Utilities.script_utils import print_dataset_splits
from Utilities.mids_utils import check_MIDS_batch
from Utilities.graph_utils import (
    create_graph_wandb,
    extract_graphs_from_batch,
    graphs_to_tuple,
)
from Utilities.gnn_models import GNNWrapper, GATLinNet, CombinedGNN, custom_gnns, premade_gnns


pd.set_option('display.max_rows', 20)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


# GLOBAL VARIABLES
BEST_MODEL_PATH = pathlib.Path(__file__).parents[0] / "Models"
BEST_MODEL_PATH.mkdir(exist_ok=True, parents=True)
BEST_MODEL_NAME = "best_model.pth"

if "PBS_O_HOME" in os.environ:
    # We are on the HPC - adjust for the CPU count and VRAM.
    BATCH_SIZE = 1/3
    NUM_WORKERS = 8
else:
    BATCH_SIZE = 0.25
    NUM_WORKERS = 8

device = "cuda" if torch.cuda.is_available() else "cpu"

class EvalType(enum.Enum):
    NONE = 0
    BASIC = 1
    DETAILED = 2
    FULL = 3


class EvalTarget(enum.Enum):
    LAST = "last"
    BEST = "best"


# ***************************************
# ************* LOSS FUNCTION ************
# ***************************************
class CustomLossFunction(torch.nn.BCEWithLogitsLoss):
    def forward(self, input: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        loss = torch.tensor(float("inf"))

        for tens in torch.split(target, input.size(dim=0)):
            new = F.binary_cross_entropy_with_logits(
                input, tens, self.weight, pos_weight=self.pos_weight, reduction=self.reduction
            )
            if new.item() < loss.item():
                loss = new
        return loss


class MinBCEWithLogitsLoss(torch.nn.Module):
    def __init__(self, reduction="mean"):
        super().__init__()
        self.bce = torch.nn.BCEWithLogitsLoss(reduction="none")  # No reduction for manual processing
        self.reduction = reduction

    def forward(self, logits, y, batch):
        """
        logits: Tensor of shape [num_nodes, 1] - Logits for all nodes
        y: Tensor of shape [num_nodes, num_options] - Padded target values (-1 for padding)
        batch: Tensor of shape [num_nodes] - Graph indices
        """
        num_nodes, num_options = y.shape
        logits_expanded = logits.unsqueeze(1).expand(-1, num_options)  # [num_nodes, num_options]

        # Compute BCE loss and mask out padding
        loss = self.bce(logits_expanded, y.float())  # [num_nodes, num_options]
        loss[y == -1] = float("inf")  # Ignore padded values

        # Compute mean loss per graph per option
        graph_ids = batch.unsqueeze(1).expand(-1, num_options)  # Broadcast batch to match shape
        mean_loss_per_graph = torch.zeros(batch.max() + 1, num_options, device=logits.device)

        mean_loss_per_graph = torch.scatter_reduce(
            mean_loss_per_graph, 0, graph_ids, loss, reduce="mean", include_self=False
        )  # Shape: [num_graphs, num_options]

        # Take the minimum mean loss across options for each graph
        min_loss_per_graph, _ = mean_loss_per_graph.min(dim=1)  # [num_graphs]

        # Apply batch reduction
        if self.reduction == "mean":
            return min_loss_per_graph.mean()
        elif self.reduction == "sum":
            return min_loss_per_graph.sum()
        return min_loss_per_graph  # Return per-graph loss if no reduction


class LossWrapper(torch.nn.Module):
    def __init__(self, criterion):
        super().__init__()
        self.criterion = criterion
        self.is_classification = not isinstance(criterion, torch.nn.L1Loss)
        self.is_batched = "batch" in inspect.signature(criterion.forward).parameters

    def forward(self, logits, y, batch):
        if self.is_batched:
            return self.criterion(logits, y, batch)
        return self.criterion(logits, y)

    @property
    def name(self):
        return self.criterion.__class__.__name__


# ***************************************
# *************** DATASET ***************
# ***************************************
def load_dataset(selected_extra_feature=None, split=0.8, batch_size=1.0, seed=42, suppress_output=False):
    # Set up dataset.
    selected_graph_sizes = {
        "03-25_mix_750": -1,
    }
    # "probabilities" for MIDS probabilities or "labels" for actual MIDS labels.
    dataset_type = "labels"
    # dataset_type = "probabilities"

    # Load the dataset.
    try:
        root = pathlib.Path(__file__).parents[0] / "Dataset"  # For standalone script.
    except NameError:
        root = pathlib.Path().cwd().parents[1] / "Dataset"  # For Jupyter notebook.
    graphs_loader = GraphDataset(selection=selected_graph_sizes, seed=seed)

    if dataset_type == "probabilities":
        dataset = MIDSProbabilitiesDataset(root, graphs_loader)
    else:
        # Training and validation `dataset` can use true, noisy or predicted probabilities as features. It can also use
        # `None` for end-to-end training without probabilities.
        dataset = MIDSLabelsDataset(root, graphs_loader, selected_extra_feature=selected_extra_feature)
        # Testing `dataset` should always use predicted probabilities as features or `None` for end-to-end testing.
        # In the first case, we simulate two GNNs stacked together. The second case is the same as using one dataset.
        sef = "predicted_probability" if selected_extra_feature != "" else ""
        evaluation_dataset = MIDSLabelsDataset(root, graphs_loader, selected_extra_feature=sef)

    # Save dataset configuration.
    dataset_config = {
        "name": type(dataset).__name__,
        "selected_graphs": str(selected_graph_sizes),
        "target": dataset.target_function.__name__,
        "split": split,
        "batch_size": batch_size,
        "seed": seed,
    }

    # Display general information about the dataset.
    if not suppress_output:
        inspect_dataset(dataset)

    # Compute any necessary or optional dataset properties.
    dataset_props = {}
    dataset_props["feature_dim"] = dataset.num_features  # type: ignore

    dataset_config["num_graphs"] = len(dataset)
    features = dataset.features

    # Shuffle the dataset.
    # TODO: Splitting after shuffle gives relatively balanced splits between the graph sizes, but it's not perfect.
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    dataset, perm = dataset.shuffle(return_perm=True)
    assert isinstance(dataset, MIDSDataset)  # We need to tell IntelliSense dataset is indeed a dataset.
    if dataset_type == "labels":
        evaluation_dataset = evaluation_dataset.index_select(perm) # type: ignore
        assert isinstance(evaluation_dataset, MIDSDataset)

    # Flexible dataset splitting. Can be split to train/test or train/val/test.
    if isinstance(dataset_config["split"], tuple):
        train_size, val_size = dataset_config["split"]
        train_size = round(train_size * len(dataset))
        val_size = round(val_size * len(dataset))
    else:
        train_size = round(dataset_config["split"] * len(dataset))
        val_size = len(dataset) - train_size

    train_dataset = dataset[:train_size]
    if val_size > 0:
        val_dataset = dataset[train_size:train_size + val_size]
    else:
        val_dataset = train_dataset
    val_size = len(val_dataset)

    if dataset_type == "labels":
        test_dataset = evaluation_dataset[train_size + val_size:]
    else:
        test_dataset = dataset[train_size + val_size:]
    test_size = len(test_dataset)

    if not suppress_output:
        print_dataset_splits(train_dataset, val_dataset, test_dataset)

    # Batch and load data.
    if dataset.target_function.__name__ == "true_labels_all_stacked":
        batch_size = 1
    elif isinstance(batch_size, float):
        max_dataset_len = max(len(train_dataset), len(val_dataset), len(test_dataset))
        batch_size = int(np.ceil(dataset_config["batch_size"] * max_dataset_len))
    train_loader = DataLoader(train_dataset, batch_size, shuffle=True, num_workers=NUM_WORKERS)  # type: ignore
    val_loader = DataLoader(val_dataset, batch_size, shuffle=False, num_workers=NUM_WORKERS)  # type: ignore
    test_loader = DataLoader(test_dataset, batch_size, shuffle=False)  # type: ignore

    # If the whole dataset fits in memory, we can use the following lines to get a single large batch.
    train_batch = next(iter(train_loader))
    val_batch = next(iter(val_loader))
    test_batch = next(iter(test_loader)) if test_size else None

    train_data_obj = train_batch if train_size <= batch_size else train_loader
    val_data_obj = val_batch if val_size <= batch_size else [val_batch for val_batch in val_loader]
    test_data_obj = test_batch if test_size <= batch_size else [test_batch for test_batch in test_loader]

    if not suppress_output:
        print()
        print("Batches:")
        print("========================================")
        for step, data in enumerate(train_loader):
            print(f"Step {step + 1}:")
            print(f"Number of graphs in the batch: {data.num_graphs}")
            print(data)
            print()

            if step == 5:
                print("The rest of batches are not displayed...")
                break
        print("========================================\n")

    return train_data_obj, val_data_obj, test_data_obj, dataset_config, features, dataset_props


# ***************************************
# ************* FUNCTIONS ***************
# ***************************************
def generate_model(architecture, in_channels, hidden_channels, num_layers, out_channels=1, **kwargs):
    """Generate a Neural Network model based on the architecture and hyperparameters."""
    # GLOBALS: device, premade_gnns, custom_gnns
    if architecture in premade_gnns:
        model = GNNWrapper(
            gnn_model=premade_gnns[architecture],
            in_channels=in_channels,
            hidden_channels=hidden_channels,
            num_layers=num_layers,
            out_channels=out_channels ,
            **kwargs,
        )
    elif architecture in custom_gnns:
        MyGNN = custom_gnns[architecture]
        model = MyGNN(in_channels, hidden_channels, num_layers, out_channels=1, **kwargs)
    else:
        model = CombinedGNN(architecture, in_channels, num_layers, out_channels=1, **kwargs)

    model = model.to(device)
    return model


def generate_optimizer(model, optimizer, lr, **kwargs):
    """Generate optimizer object based on the model and hyperparameters."""
    if optimizer == "adam":
        return torch.optim.Adam(model.parameters(), lr=lr, **kwargs)
    elif optimizer == "sgd":
        return torch.optim.SGD(model.parameters(), lr=lr, **kwargs)
    else:
        raise ValueError("Only Adam optimizer is currently supported.")


def training_pass(model, batch, optimizer, criterion) -> torch.Tensor:
    """Perform a single training pass over the batch."""
    data = batch.to(device)  # Move to CUDA if available.
    optimizer.zero_grad()  # Clear gradients.
    out = model(x=data.x, edge_index=data.edge_index, batch=data.batch)  # Perform a single forward pass.
    loss = criterion(out.squeeze(), data.y, data.batch)  # Compute the loss.
    loss.backward()  # Derive gradients.
    optimizer.step()  # Update parameters based on gradients.
    return loss


def testing_pass(model, batch, criterion):
    """Perform a single testing pass over the batch."""
    # NOTE: currently not used
    with torch.no_grad():
        data = batch.to(device)
        out = model(data.x, data.edge_index, batch=data.batch)
        loss = criterion(out.squeeze(), data.y).item()  # Compute the loss.
    return loss


def testing_pass_batch(model, batch, criterion, accuracy=False) -> tuple[torch.Tensor, torch.Tensor]:
    with torch.no_grad():
        data = batch.to(device)
        out = model(data.x, data.edge_index, batch=data.batch)
        loss = criterion(out.squeeze(), data.y, data.batch)  # Compute the loss.
        correct = torch.tensor(0, device=device)
        if accuracy:
            pred = torch.where(out.squeeze() > 0, 1.0, 0.0)
            correct = check_MIDS_batch(data, pred)
            correct = correct.int().sum()

    return loss, correct


def do_train(model, data, optimizer, criterion):
    """Train the model on individual batches or the entire dataset."""
    model.train()

    if isinstance(data, (DataLoader, list)):
        avg_loss = torch.tensor(0.0, device=device)
        for batch in data:  # Iterate in batches over the training dataset.
            avg_loss += training_pass(model, batch, optimizer, criterion)
        loss = avg_loss / len(data)
    elif isinstance(data, Data):
        loss = training_pass(model, data, optimizer, criterion)
    else:
        raise ValueError(f"Data must be a DataLoader or a Batch object, but got: {type(data)}.")

    return loss.item()


def do_test(model, data, criterion, calc_accuracy=False):
    """Test the model on individual batches or the entire dataset."""
    model.eval()

    if isinstance(data, (DataLoader, list)):
        total = 0
        correct = torch.tensor(0, device=device)
        avg_loss = torch.tensor(0.0, device=device)
        for batch in data:
            loss, corr = testing_pass_batch(model, batch, criterion, calc_accuracy)
            avg_loss += loss
            correct += corr
            total += len(batch)
        total_loss = avg_loss / len(data)
        accuracy = correct / total * 100
    elif isinstance(data, Data):
        total_loss, correct = testing_pass_batch(model, data, criterion, calc_accuracy)
        accuracy = correct / len(data) * 100
    else:
        raise ValueError(f"Data must be a DataLoader or a Batch object, but got: {type(data)}.")

    return total_loss.item(), accuracy.item()


def train(
    model, optimizer, criterion, train_data_obj, val_data_obj, num_epochs=100, suppress_output=False, save_best=False
):
    # GLOBALS: device

    # Prepare for training.
    train_losses = np.zeros(num_epochs)
    val_losses = np.zeros(num_epochs)
    train_accuracies = np.zeros(num_epochs)
    val_accuracies = np.zeros(num_epochs)
    best_loss = float("inf")
    val_loss = 0

    # This is for the hybrid approach described below.
    train_data = train_data_obj

    # Start the training loop with timer.
    training_timer = codetiming.Timer(logger=None)
    epoch_timer = codetiming.Timer(logger=None)
    training_timer.start()
    epoch_timer.start()
    for epoch in range(1, num_epochs + 1):
        # Hybrid approach for batching:
        #   run set number of epochs with one permutation and then get new batches from the DataLoader.
        if (epoch - 1) % 10 == 0 and isinstance(train_data_obj, DataLoader):
            train_data = [batch for batch in train_data_obj]

        # Perform one pass over the training set and then test on both sets.
        train_loss = do_train(model, train_data, optimizer, criterion)

        calc_accuracy = (epoch % 10 == 0 or epoch == num_epochs) and criterion.is_classification
        train_loss, train_acc = do_test(model, train_data, criterion, calc_accuracy)
        val_loss, val_acc = do_test(model, val_data_obj, criterion, calc_accuracy)

        # Store the losses.
        train_losses[epoch - 1] = train_loss
        val_losses[epoch - 1] = val_loss
        if not calc_accuracy:
            train_acc = train_accuracies[epoch - 2]
            val_acc = val_accuracies[epoch - 2]
        train_accuracies[epoch - 1] = train_acc
        val_accuracies[epoch - 1] = val_acc
        # wandb.log({"train_loss": train_loss, "val_loss": val_loss, "train_acc": train_acc, "val_acc": val_acc})

        # Save the best model.
        if save_best and epoch >= 0.3 * num_epochs and val_loss < best_loss:
            torch.save({"epoch": epoch, "model_state_dict": model.state_dict()}, BEST_MODEL_PATH)
            best_loss = val_loss

        # Print the losses every 10 epochs.
        if epoch % 10 == 0:
            avg_train_loss = sum(train_losses[epoch - 10 : epoch]) / 10
            avg_val_loss = sum(val_losses[epoch - 10 : epoch]) / 10
            avg_train_acc = train_accuracies[epoch - 1]
            avg_val_acc = val_accuracies[epoch - 1]
            wandb.log(
                {
                    "train_loss": avg_train_loss,
                    "val_loss": avg_val_loss,
                    "train_acc": avg_train_acc,
                    "val_acc": avg_val_acc,
                },
                step=epoch,
            )

            if not suppress_output:
                print(
                    f"Epoch: {epoch:03d}, "
                    f"Train Loss: {avg_train_loss:.4f}, "
                    f"Val Loss: {avg_val_loss:.4f}, "
                    f"Train Acc: {avg_train_acc:.2f}%, "
                    f"Val Acc: {avg_val_acc:.2f}%, "
                    f"Avg. duration: {epoch_timer.stop() / 10:.4f} s"
                )
                epoch_timer.start()
    epoch_timer.stop()
    duration = training_timer.stop()

    results = {
        "train_losses": train_losses,
        "val_losses": val_losses,
        "train_accuracies": train_accuracies,
        "val_accuracies": val_accuracies,
        "duration": duration,
    }
    return results


def plot_training_curves(num_epochs, train_losses, test_losses, train_accuracies, test_accuracies, criterion, title):
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(range(1, num_epochs + 1)), y=train_losses, mode="lines", name="Train Loss"))
    fig.add_trace(go.Scatter(x=list(range(1, num_epochs + 1)), y=test_losses, mode="lines", name="Val/Test Loss"))
    fig.update_layout(title=f"Training and Val/Test Loss - {title}", xaxis_title="Epoch", yaxis_title=criterion)
    fig.show()

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(range(1, num_epochs + 1)), y=train_accuracies, mode="lines", name="Train Accuracy"))
    fig.add_trace(go.Scatter(x=list(range(1, num_epochs + 1)), y=test_accuracies, mode="lines", name="Val/Test Accuracy"))
    fig.update_layout(title=f"Training and Val/Test Accuracy - {title}", xaxis_title="Epoch", yaxis_title="Accuracy (%)")
    fig.show()


def config_description(config):
    """Create a short description of the configuration."""
    desc = "{architecture}-{gnn_layers}x{hidden_channels}-{activation}-{jk}-D{dropout}-lr{learning_rate}"
    return desc.format(**config)


def eval_batch(model, batch, plot_graphs=False, is_classification=True):
    # Make predictions.
    data = batch.to(device)
    pred = model(data.x, data.edge_index, batch=data.batch)

    # Unbatch the data.
    if is_classification:
        pred = torch.where(pred > 0, 1.0, 0.0)
        dtype = np.int32
    else:
        dtype = float

    def process(x, dtype):
        r = x.cpu().numpy().T.astype(dtype)
        if dtype is float:
            r = np.round(r, 3)
        else:
            axis = 1 if len(r.shape) > 1 else None
            r = r[~np.any(r == -1, axis=axis), :]
        return r.tolist()

    predictions = [process(d, dtype) for d in tg_utils.unbatch(pred, data.batch)]
    ground_truth = [process(d, dtype) for d in tg_utils.unbatch(data.y, data.batch)]

    # Extract graphs and create visualizations.
    # TODO: These two lines are very slow.
    nx_graphs = extract_graphs_from_batch(data)
    graphs, node_nums, edge_nums = zip(*graphs_to_tuple(nx_graphs))
    # FIXME: This is the only way to parallelize in Jupyter but runs out of memory.
    # with concurrent.futures.ProcessPoolExecutor(4) as executor:
    #     graph_visuals = executor.map(create_graph_wandb, nx_graphs, chunksize=10)
    if plot_graphs:
        graph_visuals = [create_graph_wandb(g) for g in nx_graphs]
    else:
        graph_visuals = ["N/A"] * len(nx_graphs)

    # Store to pandas DataFrame.
    return pd.DataFrame(
        {
            "GraphVis": graph_visuals,
            "Graph": graphs,
            "Nodes": node_nums,
            "Edges": edge_nums,
            "True": ground_truth,
            "Predicted": predictions,
        }
    )


def evaluate(
    model, epoch, criterion, train_data, test_data, plot_graphs=False, make_table=False, suppress_output=False
):
    model.eval()
    df = pd.DataFrame()

    # Loss on the train and test set.
    train_loss, train_acc = do_test(model, train_data, criterion, criterion.is_classification)
    test_loss, test_acc = do_test(model, test_data, criterion, criterion.is_classification)

    # Build a detailed results DataFrame.
    with torch.no_grad():
        if isinstance(test_data, (DataLoader, list)):
            for batch in test_data:
                df = pd.concat([df, eval_batch(model, batch, plot_graphs, criterion.is_classification)])
        elif isinstance(test_data, Data):
            df = eval_batch(model, test_data, plot_graphs, criterion.is_classification)
        else:
            raise ValueError(f"Data must be a DataLoader or a Batch object, but got: {type(test_data)}.")

    # Create a W&B table.
    table = wandb.Table(dataframe=df) if make_table else None

    # Analyze the results.
    avg_error = avg_abs_error = std_error = np.nan
    if not criterion.is_classification:
        # Calculate differences and statistics
        errors = np.concatenate(
            [(np.array(row["Predicted"]) - np.array(row["True"])).squeeze() for _, row in df.iterrows()]
        )

        avg_error = np.mean(errors)
        avg_abs_error = np.mean(np.abs(errors))
        std_error = np.std(errors)

    # Print and plot.
    if not suppress_output:
        print(f"Evaluating model at epoch {epoch}.\n")
        print(
            f"Train loss: {train_loss:.8f} (averaged over batches)\n"
            f"Test loss : {test_loss:.8f}\n"
            f"Train accuracy: {train_acc:.2f}%\n"
            f"Test accuracy : {test_acc:.2f}%\n"
            f"\n"
            f"Mean error     : {avg_error:.4f} (all examples)\n"
            f"Mean abs. error: {avg_abs_error:.4f}\n"
            f"Std. dev. error: {std_error:.4f}\n"
        )
        df = df.sort_values(by="Nodes")
        print("\nDetailed results:")
        print("==================")
        print(df.head(10))

    results = {
        "loss": test_loss,
        "accuracy": test_acc,
        "table": table,
        "mean_err": avg_error,
        "mean_abs_err": avg_abs_error,
        "std_err": std_error,
    }
    return results


def main(config=None, eval_type=EvalType.NONE, eval_target=EvalTarget.LAST, no_wandb=False, is_best_run=False):
    # GLOBALS: device

    # Helper boolean flags.
    save_best = eval_target == EvalTarget.BEST
    plot_graphs = eval_type == EvalType.FULL
    make_table = eval_type.value > EvalType.BASIC.value

    # Tags for W&B.
    is_sweep = config is None
    wandb_mode = "disabled" if no_wandb else "online"
    tags = ["end_to_end"]
    if is_best_run:
        tags.append("BEST")

    # Set up the run
    run = wandb.init(mode=wandb_mode, entity="LARICS-GNN", project="MIDS-GNN", tags=tags, config=config)
    config = wandb.config
    if is_sweep:
        print(f"Running sweep with config: {config}...")

    if "PBS_O_HOME" in os.environ:
        # We are on the HPC - paralel runs use the same disk.
        global BEST_MODEL_PATH
        BEST_MODEL_PATH /= f"{run.id}_{BEST_MODEL_NAME}"
    else:
        BEST_MODEL_PATH /= BEST_MODEL_NAME

    # Set up the default model configuration.
    model_kwargs = {}
    if config["architecture"] == "GIN":
        model_kwargs = {"train_eps": True}
    elif config["architecture"] == "GAT":
        model_kwargs = {"v2": True}

    model_kwargs.update(config.get("model_kwargs", {}))

    # Load the dataset.
    train_data_obj, val_data_obj, test_data_obj, dataset_config, features, dataset_props = load_dataset(
        selected_extra_feature=config.get("selected_extra_feature"),
        batch_size=BATCH_SIZE,
        split=config.get("dataset", {}).get("split", (0.6, 0.2)),
        suppress_output=is_sweep,
    )

    wandb.config["dataset"] = dataset_config
    if "selected_features" not in wandb.config or not wandb.config["selected_features"]:
        wandb.config["selected_features"] = features

    # Set up the model, optimizer, and criterion.
    model = generate_model(
        config["architecture"],
        dataset_props["feature_dim"],
        config["hidden_channels"],
        config["gnn_layers"],
        act=config["activation"],
        dropout=float(config["dropout"]),
        jk=config["jk"] if config["jk"] != "none" else None,
        **model_kwargs,
    )
    optimizer = generate_optimizer(model, config["optimizer"], config["learning_rate"])
    criterion_options = {
        "true_labels_all_padded": MinBCEWithLogitsLoss,
        "true_labels_all_stacked": CustomLossFunction,
        "true_labels_single": torch.nn.BCEWithLogitsLoss,
        "true_probabilities": torch.nn.L1Loss,
    }
    criterion = LossWrapper(criterion_options[dataset_config["target"]]())

    wandb.watch(model, criterion, log="all", log_freq=100)

    # Run training.
    print("Training...")
    print("===========")
    train_results = train(
        model,
        optimizer,
        criterion,
        train_data_obj,
        val_data_obj,
        config["epochs"],
        suppress_output=is_sweep,
        save_best=save_best,
    )
    run.summary["best_train_loss"] = min(train_results["train_losses"])
    run.summary["best_val_loss"] = min(train_results["val_losses"])
    run.summary["best_train_accuracy"] = max(train_results["train_accuracies"])
    run.summary["best_val_accuracy"] = max(train_results["val_accuracies"])
    run.summary["duration"] = train_results["duration"]
    if not is_sweep:
        plot_training_curves(
            config["epochs"],
            train_results["train_losses"],
            train_results["val_losses"],
            train_results["train_accuracies"],
            train_results["val_accuracies"],
            criterion.name,
            config_description(config),
        )

    # Run evaluation.
    if eval_type != EvalType.NONE:
        epoch = config["epochs"]
        if eval_target == EvalTarget.BEST:
            checkpoint = torch.load(BEST_MODEL_PATH, weights_only=False)
            model.load_state_dict(checkpoint["model_state_dict"])
            epoch = checkpoint["epoch"]

        print("\nEvaluation results:")
        print("===================")
        eval_results = evaluate(
            model,
            epoch,
            criterion,
            train_data_obj,
            test_data_obj or val_data_obj,
            plot_graphs,
            make_table,
            suppress_output=is_sweep,
        )
        run.summary["eval_loss"] = eval_results["loss"]
        run.summary["eval_accuracy"] = eval_results["accuracy"]
        run.summary["mean_error"] = eval_results["mean_err"]
        run.summary["mean_abs_error"] = eval_results["mean_abs_err"]
        run.summary["std_error"] = eval_results["std_err"]

        if eval_type.value > EvalType.BASIC.value:
            run.log({"results_table": eval_results["table"]})

        if is_best_run:
            # Name the model with the current time and date to make it unique.
            model_name = f"{config_description(config)}-{datetime.datetime.now().strftime('%d%m%y_%H%M')}"
            artifact = wandb.Artifact(name=model_name, type="model")
            artifact.add_file(str(BEST_MODEL_PATH))
            run.log_artifact(artifact)
            # Save the complete model that can be used in other runs or scripts.
            wandb.unwatch()
            torch.save(model, BEST_MODEL_PATH)

    if is_sweep:
        print("    ...DONE.")
        if eval_type != EvalType.NONE:
            print(f"Loss: {eval_results['loss']:.8f}")
            print(f"Accuracy: {eval_results['accuracy']:.2f}%")
        print(f"Duration: {train_results['duration']:.8f} s.")
    return run


if __name__ == "__main__":
    args = argparse.ArgumentParser(description="Train a GNN model to predict the algebraic connectivity of graphs.")
    args.add_argument("--standalone", action="store_true", help="Run the script as a standalone.")
    # These are the options for model performance evaluation.
    # - none: Evaluation is skipped.
    # - basic: Will calculate all metrics and plot the graphs, but will not upload the results table to W&B.
    # - detailed: Same as basic, but will also upload the results table to W&B.
    # - full: Same as detailed, but will also plot the graphs inside the results table.
    args.add_argument(
        "--eval-type",
        action="store",
        choices=["basic", "detailed", "full", "none"],
        help="Level of detail for model evaluation.",
    )
    # Evaluate the model from the last epoch or the best.
    args.add_argument(
        "--eval-target", action="store", choices=["best", "last"], default="last", help="Which model to evaluate."
    )
    args.add_argument("--no-wandb", action="store_true", help="Do not use W&B for logging.")
    # If best is set, the script will evaluate the best model, add the BEST tag, plot the graphs inside the results
    # table and upload everything to W&B.
    args.add_argument("--best", action="store_true", help="Mark and store the best model.")
    args = args.parse_args()

    eval_type = EvalType[args.eval_type.upper()] if args.eval_type else EvalType.NONE
    eval_target = EvalTarget[args.eval_target.upper()]

    print(f"Loaded torch. Using *{device}* device.")

    if args.standalone:
        global_config = {
            ## Model configuration
            "architecture": "GAT+GAT",
            "hidden_channels": 0,
            "gnn_layers": 5,
            "activation": "N/A",
            "jk": "cat",
            "dropout": 0.0,
            ## Training configuration
            "optimizer": "adam",
            "learning_rate": 0.01,
            "epochs": 750,
            ## Dataset configuration
            "selected_extra_feature": "",
        }
        run = main(global_config, eval_type, eval_target, args.no_wandb, args.best)
        run.finish()
    else:
        run = main(None, eval_type, eval_target, False, args.best)
