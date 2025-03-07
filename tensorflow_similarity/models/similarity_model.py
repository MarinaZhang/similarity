# Copyright 2021 The TensorFlow Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Similarity model used for metric learning.

Subclass of Keras.Model which provides methods for indexing, matching,
evaluation, and similarity training.

```python
from tf.keras import layers
from tensorflow_similarity.models import SimilarityModel

# Setup dataset using tf.sim samplers
train_ds = ...

# Create similarity loss
similarity_loss = ...

# Build the model
inputs = layers.Input(shape=(28,28,1))
x = layers.Conv2D(32, 3, activation='relu')(x)
x = layers.Flatten()(x)
outputs = layers.Dense(16, activation='relu')(x)

model = SimilarityModel(inputs, outputs)
model.compile(optimizer=Adam(LR), loss=similarity_loss)

history = model.fit(train_ds)
```
"""
from __future__ import annotations

from collections import defaultdict
from copy import copy
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import tensorflow as tf
from tabulate import tabulate
from tqdm.auto import tqdm

from .. import distances
from ..classification_metrics import make_classification_metric
from ..indexer import Indexer

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping, MutableSequence, Sequence

    from tensorflow.keras.losses import Loss
    from tensorflow.keras.metrics import Metric
    from tensorflow.keras.optimizers import Optimizer

    from ..classification_metrics import ClassificationMetric
    from ..distances import Distance
    from ..evaluators.evaluator import Evaluator
    from ..losses import MetricLoss
    from ..matchers import ClassificationMatch
    from ..retrieval_metrics import RetrievalMetric
    from ..search import Search
    from ..stores import Store
    from ..training_metrics import DistanceMetric
    from ..types import (
        CalibrationResults,
        FloatTensor,
        IntTensor,
        Lookup,
        PandasDataFrame,
        Tensor,
    )


@tf.keras.utils.register_keras_serializable(package="Similarity")
class SimilarityModel(tf.keras.Model):
    """Specialized Keras.Model which implement the core features needed for
    metric learning. In particular, `SimilarityModel()` supports indexing,
    searching and saving the embeddings predicted by the network.

    All Similarity models classes derive from this class to benefits from those
    core features.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def compile(
        self,
        optimizer: Optimizer | str | Mapping | Sequence = "rmsprop",
        loss: Loss | MetricLoss | str | Mapping | Sequence | None = None,
        metrics: Metric | DistanceMetric | str | Mapping | Sequence | None = None,  # noqa
        loss_weights: Mapping | Sequence | None = None,
        weighted_metrics: Metric | DistanceMetric | str | Mapping | Sequence | None = None,  # noqa
        run_eagerly: bool = False,
        steps_per_execution: int = 1,
        distance: Distance | str = "auto",
        embedding_output: int | None = None,
        kv_store: Store | str = "memory",
        search: Search | str = "linear",
        evaluator: Evaluator | str = "memory",
        stat_buffer_size: int = 1000,
        **kwargs,
    ):
        """Configures the model for training.

        Args:
            optimizer: String (name of optimizer) or optimizer instance. See
              [tf.keras.optimizers](https://www.tensorflow.org/api_docs/python/tf/keras/optimizers).

            loss: String (name of objective function), objective function, any
              `tensorflow_similarity.loss.*` instance or a `tf.keras.losses.Loss`
              instance. See the [Losses documentation](../losses.md) for a list of
              metric learning specific losses offered by TensorFlow Similairy and
              [tf.keras.losses](https://www.tensorflow.org/api_docs/python/tf/keras/losses)
              for the losses available directly in TensorFlow.

            metrics: List of metrics to be evaluated by the model during
              training and testing. Each of those can be a string, a function or a
              [tensorflow_similairty.metrics.*](../metrics.md) instance. Note that
              the metrics used for some type of metric-learning such as distance
              learning (e.g via triplet loss) have a different prototype than the
              metrics used in standard models and you can't use the
              `tf.keras.metrics` for those type of learning.

              Additionally many distance metrics are computed based of the
              [Indexer()](../indexer.md) performance. E.g Matching Top 1 accuracy.
              For technical and performance reasons, indexing data at each
              training batch to compute those is impractical so those metrics are
              computed at epoch end via the [EvalCallback](../callbacks.md)

              See [Evaluation Metrics](../eval_metrics.md) for a list of available
              metrics.

              For multi-output models you can specify different metrics for
              different outputs by passing a dictionary, such as
              `metrics={'similarity': 'min_neg_gap', 'other': ['accuracy',
              'mse']}`.  You can also pass a list (len = len(outputs)) of lists of
              metrics such as `metrics=[['min_neg_gap'], ['accuracy', 'mse']]` or
              `metrics=['min_neg_gap', ['accuracy', 'mse']]`. For outputs which
              are not related to metrics learning, you can use any of the standard
              `tf.keras.metrics`.

            loss_weights: Optional list or dictionary specifying scalar
              coefficients (Python floats) to weight the loss contributions of
              different model outputs. The loss value that will be minimized by
              the model will then be the *weighted sum* of all individual losses,
              weighted by the `loss_weights` coefficients.  If a list, it is
              expected to have a 1:1 mapping to the model's outputs. If a dict, it
              is expected to map output names (strings) to scalar coefficients.

            weighted_metrics: List of metrics to be evaluated and weighted by
              sample_weight or class_weight during training and testing.

            run_eagerly: Bool. Defaults to `False`. If `True`, this `Model`'s
              logic will not be wrapped in a `tf.function`. Recommended to leave
              this as `None` unless your `Model` cannot be run inside a
              `tf.function`.

            steps_per_execution: Int. Defaults to 1. The number of batches to
              run during each `tf.function` call. Running multiple batches inside
              a single `tf.function` call can greatly improve performance on TPUs
              or small models with a large Python overhead.  At most, one full
              epoch will be run each execution. If a number larger than the size
              of the epoch is passed,  the execution will be truncated to the size
              of the epoch.  Note that if `steps_per_execution` is set to `N`,
              `Callback.on_batch_begin` and `Callback.on_batch_end` methods will
              only be called every `N` batches (i.e. before/after each
              `tf.function` execution).

            distance: Distance used to compute embeddings proximity.  Defaults
              to 'cosine'.

            embedding_output: Which model output head predicts the embeddings
              that should be indexed. Default to None which is for single output
              model. For multi-head model, the callee, usually the
              `SimilarityModel()` class is responsible for passing the correct
              one.

            kv_store: How to store the indexed records.  Defaults to 'memory'.

            search: Which `Search()` framework to use to perform KNN search.
              Defaults to 'linear'.

            evaluator: What type of `Evaluator()` to use to evaluate index
              performance. Defaults to in-memory one.

            stat_buffer_size: Size of the sliding windows buffer used to compute
              index performance. Defaults to 1000.

        Raises:
            ValueError: In case of invalid arguments for
                `optimizer`, `loss` or `metrics`.
        """
        # Fetching the distance used from the first loss if auto
        if distance == "auto":
            if loss is None:
                distance = distances.get("cosine")
            else:
                metric_loss = loss[0] if isinstance(loss, list) else loss

                try:
                    distance = metric_loss.distance
                except AttributeError:
                    msg = "distance='auto' only works if the first loss is a " "metric loss"
                    raise ValueError(msg)

            print(f"Distance metric automatically set to {distance} use the " "distance arg to override.")
        else:
            distance = distances.get(distance)

        # init index
        self.create_index(
            distance=distance,
            search=search,
            kv_store=kv_store,
            evaluator=evaluator,
            embedding_output=embedding_output,
            stat_buffer_size=stat_buffer_size,
        )

        # call underlying keras method
        if tf.executing_eagerly():
            super().compile(
                optimizer=optimizer,
                loss=loss,
                metrics=metrics,
                loss_weights=loss_weights,
                weighted_metrics=weighted_metrics,
                run_eagerly=run_eagerly,
                steps_per_execution=steps_per_execution,
                **kwargs,
            )
        else:
            # apprently keras calls training_v1.py in graph mode which does not
            # support the steps_per_execution arg.
            super().compile(
                optimizer=optimizer,
                loss=loss,
                metrics=metrics,
                loss_weights=loss_weights,
                weighted_metrics=weighted_metrics,
                run_eagerly=run_eagerly,
                **kwargs,
            )

    @property
    def _index(self) -> Indexer:
        if not hasattr(self, "_ix") and self._ix:
            raise AttributeError(
                "The model does not currently have an index. To create an "
                "index you must call either model.compile() or "
                "model.create_index() and set a valid Distance."
            )

        return self._ix

    @_index.setter
    def _index(self, index: Indexer) -> None:
        self._ix = index

    def create_index(
        self,
        distance: Distance | str = "cosine",
        search: Search | str = "linear",
        kv_store: Store | str = "memory",
        evaluator: Evaluator | str = "memory",
        embedding_output: int | None = None,
        stat_buffer_size: int = 1000,
    ) -> None:
        """Create the model index to make embeddings searchable via KNN.

        This method is normally called as part of `SimilarityModel.compile()`.
        However, this method is provided if users want to define a custom index
        outside of the `compile()` method.

        NOTE: This method sets `SimilarityModel._index` and will replace any
        existing index.

        Args:
            distance: Distance used to compute embeddings proximity. Defaults to
            'auto'.

            kv_store: How to store the indexed records.  Defaults to 'memory'.

            search: Which `Search()` framework to use to perform KNN search.
            Defaults to 'linear'.

            evaluator: What type of `Evaluator()` to use to evaluate index
            performance. Defaults to in-memory one.

            embedding_output: Which model output head predicts the embeddings
            that should be indexed. Default to None which is for single output
            model. For multi-head model, the callee, usually the
            `SimilarityModel()` class is responsible for passing the correct
            one.

            stat_buffer_size: Size of the sliding windows buffer used to compute
            index performance. Defaults to 1000.

        Raises:
            ValueError: Invalid search framework or key value store.
        """
        # check if we we need to set the embedding head
        num_outputs = len(self.output_names)
        if embedding_output is not None and embedding_output > num_outputs:
            raise ValueError("Embedding_output value exceed number of model outputs")

        if embedding_output is None and num_outputs > 1:
            print(
                "Embedding output set to be model output 0. ",
                "Use the embedding_output arg to override this.",
            )
            embedding_output = 0

        # fetch embedding size as some ANN libs requires it for init
        if num_outputs > 1:
            self.embedding_size = self.outputs[embedding_output].shape[1]
        else:
            self.embedding_size = self.outputs[0].shape[1]

        self._index = Indexer(
            embedding_size=self.embedding_size,
            distance=distance,
            search=search,
            kv_store=kv_store,
            evaluator=evaluator,
            embedding_output=embedding_output,
            stat_buffer_size=stat_buffer_size,
        )

    def index(
        self,
        x: Tensor,
        y: IntTensor | None = None,
        data: Tensor | None = None,
        build: bool = True,
        verbose: int = 1,
    ):
        """Index data.

        Args:
            x: Samples to index.

            y: class ids associated with the data if any. Defaults to None.

            data: store the data associated with the samples in the key
            value store. Defaults to True.

            build: Rebuild the index after indexing. This is needed to make the
            new samples searchable. Set it to false to save processing time
            when calling indexing repeatidly without the need to search between
            the indexing requests. Defaults to True.

            verbose: Output indexing progress info. Defaults to 1.
        """

        if verbose:
            print(f"[Indexing {len(x)} points]")
            print("|-Computing embeddings")

        predictions = self.predict(x)

        self._index.batch_add(
            predictions=predictions,
            labels=y,
            data=data,
            build=build,
            verbose=verbose,
        )

    def index_single(
        self,
        x: Tensor,
        y: IntTensor | None = None,
        data: Tensor | None = None,
        build: bool = True,
        verbose: int = 1,
    ):
        """Index data.

        Args:
            x: Sample to index.

            y: class id associated with the data if any. Defaults to None.

            data: store the data associated with the samples in the key
            value store. Defaults to None.

            build: Rebuild the index after indexing. This is needed to make the
            new samples searchable. Set it to false to save processing time
            when calling indexing repeatidly without the need to search between
            the indexing requests. Defaults to True.

            verbose: Output indexing progress info. Defaults to 1.
        """

        if verbose:
            print("[Indexing 1 point]")
            print("|-Computing embeddings")

        x = tf.expand_dims(x, axis=0)
        prediction = self.predict(x)

        self._index.add(
            prediction=prediction,
            label=y,
            data=data,
            build=build,
            verbose=verbose,
        )

    def lookup(self, x: Tensor, k: int = 5, verbose: int = 1) -> list[list[Lookup]]:
        """Find the k closest matches in the index for a set of samples.

        Args:
            x: Samples to match.

            k: Number of nearest neighboors to lookup. Defaults to 5.

            verbose: display progress. Default to 1.

        Returns
            list of list of k nearest neighboors:
            list[list[Lookup]]
        """
        predictions = self.predict(x)

        return self._index.batch_lookup(predictions=predictions, k=k, verbose=verbose)

    def single_lookup(self, x: Tensor, k: int = 5) -> list[Lookup]:
        """Find the k closest matches in the index for a given sample.

        Args:
            x: Sample to match.

            k: Number of nearest neighboors to lookup. Defaults to 5.

        Returns
            list of the k nearest neigboors info:
            list[Lookup]
        """
        x = tf.expand_dims(x, axis=0)
        prediction = self.predict(x)

        return self._index.single_lookup(prediction=prediction, k=k)

    def index_summary(self):
        "Display index info summary."
        self._index.print_stats()

    def calibrate(
        self,
        x: FloatTensor,
        y: IntTensor,
        thresholds_targets: MutableMapping[str, float] | None = None,
        k: int = 1,
        calibration_metric: str | ClassificationMetric = "f1",
        matcher: str | ClassificationMatch = "match_nearest",
        extra_metrics: MutableSequence[str | ClassificationMetric] = [
            "precision",
            "recall",
        ],  # noqa
        rounding: int = 2,
        verbose: int = 1,
    ) -> CalibrationResults:
        """Calibrate model thresholds using a test dataset.

        TODO: more detailed explaination.

        Args:

            x: examples to use for the calibration.

            y: labels associated with the calibration examples.

            thresholds_targets: Dict of performance targets to (if possible)
            meet with respect to the `calibration_metric`.

            calibration_metric:
            [ClassificationMetric()](classification_metrics/overview.md) used
            to evaluate the performance of the index.

            k: How many neighboors to use during the calibration.
            Defaults to 1.

            matcher: {'match_nearest', 'match_majority_vote'} or
            ClassificationMatch object. Defines the classification matching,
            e.g., match_nearest will count a True Positive if the query_label
            is equal to the label of the nearest neighbor and the distance is
            less than or equal to the distance threshold. Defaults to
            'match_nearest'.

            extra_metrics: List of additional
            `tf.similarity.classification_metrics.ClassificationMetric()`
            to compute and report. Defaults to ['precision', 'recall'].


            rounding: Metric rounding. Default to 2 digits.

            verbose: Be verbose and display calibration results.
            Defaults to 1.

        Returns:
            CalibrationResults containing the thresholds and cutpoints Dicts.
        """
        if thresholds_targets is None:
            thresholds_targets = {}

        # predict
        predictions = self.predict(x)

        # calibrate
        return self._index.calibrate(
            predictions=predictions,
            target_labels=y,
            thresholds_targets=thresholds_targets,
            k=k,
            calibration_metric=calibration_metric,
            matcher=matcher,
            extra_metrics=extra_metrics,
            rounding=rounding,
            verbose=verbose,
        )

    def match(
        self,
        x: FloatTensor,
        cutpoint="optimal",
        no_match_label=-1,
        k=1,
        matcher: str | ClassificationMatch = "match_nearest",
        verbose=0,
    ):
        """Match a set of examples against the calibrated index

        For the match function to work, the index must be calibrated using
        calibrate().

        Args:
            x: Batch of examples to be matched against the index.

            cutpoint: Which calibration threshold to use.
            Defaults to 'optimal' which is the optimal F1 threshold computed
            using calibrate().

            no_match_label: Which label value to assign when there is no
            match. Defaults to -1.

            k: How many neighboors to use during the calibration.
            Defaults to 1.

            matcher: {'match_nearest', 'match_majority_vote'} or
            ClassificationMatch object. Defines the classification matching,
            e.g., match_nearest will count a True Positive if the query_label
            is equal to the label of the nearest neighbor and the distance is
            less than or equal to the distance threshold.

            verbose. Be verbose. Defaults to 0.

        Returns:
            List of class ids that matches for each supplied example

        Notes:
            This function matches all the cutpoints at once internally as there
            is little performance downside to do so and allows to do the
            evaluation in a single go.

        """
        # basic checks
        if not self._index.is_calibrated:
            raise ValueError("Uncalibrated model: run model.calibration()")

        # get predictions
        predictions = self.predict(x)

        # matching
        matches = self._index.match(
            predictions,
            no_match_label=no_match_label,
            k=k,
            matcher=matcher,
            verbose=verbose,
        )

        # select which matches to return
        if cutpoint == "all":  # returns all the cutpoints for eval purpose.
            return matches
        else:  # normal match behavior - returns a specific cut point
            return matches[cutpoint]

    def evaluate_retrieval(
        self,
        x: Tensor,
        y: IntTensor,
        retrieval_metrics: Sequence[RetrievalMetric],  # noqa
        verbose: int = 1,
    ) -> dict[str, np.ndarray]:
        """Evaluate the quality of the index against a test dataset.

        Args:
            x: Examples to be matched against the index.

            y: Label associated with the examples supplied.

            retrieval_metrics: List of
            [RetrievalMetric()](retrieval_metrics/overview.md) to compute.

            verbose (int, optional): Display results if set to 1 otherwise
            results are returned silently. Defaults to 1.

        Returns:
            Dictionary of metric results where keys are the metric names and
            values are the metrics values.

        Raises:
            IndexError: Index must contain embeddings but is currently empty.
        """
        if self._index.size() == 0:
            raise IndexError("Index must contain embeddings but is " "currently empty. Have you run model.index()?")

        # get embeddings
        if verbose:
            print("|-Computing embeddings")
        predictions = self.predict(x)

        if verbose:
            print("|-Computing retrieval metrics")

        results = self._index.evaluate_retrieval(
            predictions=predictions,
            target_labels=y,
            retrieval_metrics=retrieval_metrics,
            verbose=verbose,
        )

        if verbose:
            table = zip(results.keys(), results.values())
            headers = ["metric", "Value"]
            print("\n [Summary]\n")
            print(tabulate(table, headers=headers))

        return results

    def evaluate_classification(
        self,
        x: Tensor,
        y: IntTensor,
        k: int = 1,
        extra_metrics: MutableSequence[str | ClassificationMetric] = [
            "precision",
            "recall",
        ],  # noqa
        matcher: str | ClassificationMatch = "match_nearest",
        verbose: int = 1,
    ) -> defaultdict[str, dict[str, str | np.ndarray]]:
        """Evaluate model classification matching on a given evaluation dataset.

        Args:
            x: Examples to be matched against the index.

            y: Label associated with the examples supplied.

            k: How many neighbors to use to perform the evaluation.
            Defaults to 1.

            extra_metrics: List of additional
            `tf.similarity.classification_metrics.ClassificationMetric()` to
            compute and report. Defaults to ['precision', 'recall'].

            matcher: {'match_nearest', 'match_majority_vote'} or
            ClassificationMatch object. Defines the classification matching,
            e.g., match_nearest will count a True Positive if the query_label
            is equal to the label of the nearest neighbor and the distance is
            less than or equal to the distance threshold.

            verbose (int, optional): Display results if set to 1 otherwise
            results are returned silently. Defaults to 1.

        Returns:
            Dictionary of (distance_metrics.md)[evaluation metrics]

        Raises:
            IndexError: Index must contain embeddings but is currently empty.
            ValueError: Uncalibrated model: run model.calibration()")
        """
        # There is some code duplication in this function but that is the best
        # solution to keep the end-user API clean and doing inferences once.
        if self._index.size() == 0:
            raise IndexError(
                "Index must contain embeddings but is currently empty. Have " "you added examples via model.index()?"
            )

        if not self._index.is_calibrated:
            raise ValueError("Uncalibrated model: run model.calibration()")

        cal_metric = self._index.get_calibration_metric()

        # get embeddings
        if verbose:
            print("|-Computing embeddings")
        predictions = self.predict(x)

        results: defaultdict[str, dict[str, str | np.ndarray]] = defaultdict(dict)

        if verbose:
            pb = tqdm(total=len(self._index.cutpoints), desc="Evaluating cutpoints")

        for cp_name, cp_data in self._index.cutpoints.items():
            # create a metric that match at the requested k and threshold
            distance_threshold = float(cp_data["distance"])
            metric = make_classification_metric(cal_metric.name)
            metrics = copy(extra_metrics)
            metrics.append(metric)

            res: dict[str, str | np.ndarray] = {}
            res.update(
                self._index.evaluate_classification(
                    predictions,
                    y,
                    [distance_threshold],
                    metrics=metrics,
                    matcher=matcher,
                    k=k,
                )
            )
            res["distance"] = tf.constant([distance_threshold], dtype=tf.keras.backend.floatx())
            res["name"] = cp_name
            results[cp_name] = res
            if verbose:
                pb.update()

        if verbose:
            pb.close()

        if verbose:
            headers = ["name", cal_metric.name]
            for i in results["optimal"].keys():
                if i not in headers:
                    headers.append(str(i))
            rows = []
            for data in results.values():
                rows.append([data[v] for v in headers])
            print("\n [Summary]\n")
            print(tabulate(rows, headers=headers))

        return results

    def reset_index(self):
        "Reinitialize the index"
        self._index.reset()

    def index_size(self) -> int:
        "Return the index size"
        return self._index.size()

    def load_index(self, filepath: str):
        """Load Index data from a checkpoint and initialize underlying
        structure with the reloaded data.

        Args:
            path: Directory where the checkpoint is located.
            verbose: Be verbose. Defaults to 1.
        """

        index_path = Path(filepath) / "index"
        self._index = Indexer.load(index_path)

    def save_index(self, filepath, compression=True, overwrite=True):
        """Save the index to disk

        Args:
            path: directory where to save the index
            compression: Store index data compressed. Defaults to True.
            overwrite: Overwrite previous index. Defaults to False.
        """
        index_path = Path(filepath) / "index"
        self._index.save(index_path, compression=compression, overwrite=overwrite)

    def save(
        self,
        filepath: str,
        save_index: bool = True,
        compression: bool = True,
        overwrite: bool = True,
        include_optimizer: bool = True,
        save_format: str | None = None,
        signatures=None,
        options=None,
        save_traces: bool = True,
    ):
        """Save the model and the index.

        Args:
            filepath: where to save the model.

            save_index: Save the index content. Defaults to True.

            compression: Compress index data. Defaults to True.

            overwrite: Overwrite previous model. Defaults to True.

            include_optimizer: Save optimizer state. Defaults to True.

            save_format: Either 'tf' or 'h5', indicating whether to save the
            model to Tensorflow SavedModel or HDF5. Defaults to 'tf' in
            TF 2.X, and 'h5' in TF 1.X.

            signatures: Signatures to save with the model. Defaults to None.

            options: A `tf.saved_model.SaveOptions` to save with the model.
            Defaults to None.

            save_traces (optional): When enabled, the SavedModel will
            store the function traces for each layer. This can be disabled,
            so that only the configs of each layer are stored.
            Defaults to True. Disabling this will decrease serialization
            time and reduce file size, but it requires that all
            custom layers/models implement a get_config() method.
        """

        # call underlying keras method to save the mode graph and its weights
        super().save(
            filepath,
            overwrite=overwrite,
            include_optimizer=include_optimizer,
            save_format=save_format,
            signatures=signatures,
            options=options,
            save_traces=save_traces,
        )

        if hasattr(self, "_ix") and self._index and save_index:
            self.save_index(filepath, compression=compression, overwrite=overwrite)
        else:
            msg = "The index was not saved with the model."
            if not hasattr(self, "_ix"):
                msg = msg + (
                    "The model does not currently have an index. To use indexing "
                    "you must call either model.compile() or model.create_index() "
                    "and set a valid Distance."
                )

            if not save_index:
                msg = msg + " The save_index param is set to False."

            print(msg)

    def to_data_frame(self, num_items: int = 0) -> PandasDataFrame:
        """Export data as pandas dataframe

        Args:
            num_items (int, optional): Num items to export to the dataframe.
            Defaults to 0 (unlimited).

        Returns:
            pd.DataFrame: a pandas dataframe.
        """
        return self._index.to_data_frame(num_items=num_items)

    # We don't need from_config as the index is reloaded separatly.
    # this is kept as a reminder that it was looked into and decided to split
    # the index reloading instead of overloading this method.
    # @classmethod
    # def from_config(cls, config):
    #     return super().from_config(**config)
