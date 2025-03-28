"""
Laboratory work.

Working with Large Language Models.
"""
# pylint: disable=too-few-public-methods, undefined-variable, too-many-arguments, super-init-not-called
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import torch
from datasets import load_dataset
from evaluate import load
from pandas import DataFrame
from torch.utils.data import DataLoader, Dataset
from torchinfo import summary
from transformers import AutoModelForSeq2SeqLM, T5TokenizerFast

from core_utils.llm.llm_pipeline import AbstractLLMPipeline
from core_utils.llm.metrics import Metrics
from core_utils.llm.raw_data_importer import AbstractRawDataImporter
from core_utils.llm.raw_data_preprocessor import AbstractRawDataPreprocessor, ColumnNames
from core_utils.llm.task_evaluator import AbstractTaskEvaluator
from core_utils.llm.time_decorator import report_time


class RawDataImporter(AbstractRawDataImporter):
    """
    A class that imports the HuggingFace dataset.
    """

    @report_time
    def obtain(self) -> None:
        """
        Download a dataset.

        Raises:
            TypeError: In case of downloaded dataset is not pd.DataFrame
        """
        self._raw_data = load_dataset(self._hf_name, split='test',
                                      revision='v2.0', trust_remote_code=True).to_pandas()


class RawDataPreprocessor(AbstractRawDataPreprocessor):
    """
    A class that analyzes and preprocesses a dataset.
    """

    def analyze(self) -> dict:
        """
        Analyze a dataset.

        Returns:
            dict: Dataset key properties
        """
        n_empty_rows = len(self._raw_data) - len(self._raw_data.replace('', pd.NA).dropna())

        ds_lens = self._raw_data.text.apply(len)

        ds_properties = {
            'dataset_number_of_samples': len(self._raw_data),
            'dataset_columns': len(self._raw_data.columns),
            'dataset_duplicates': int(self._raw_data.duplicated().sum()),
            'dataset_empty_rows': n_empty_rows,
            'dataset_sample_min_len': int(ds_lens.min()),
            'dataset_sample_max_len': int(ds_lens.max())
        }

        return ds_properties

    @report_time
    def transform(self) -> None:
        """
        Apply preprocessing transformations to the raw dataset.
        """
        self._data = (
            self._raw_data
            .drop(columns=['title', 'date', 'url'])
            .rename(columns={'text': ColumnNames.SOURCE.name, 'summary': ColumnNames.TARGET.name})
            .replace('', pd.NA)
            .dropna()
            .drop_duplicates()
            .reset_index(drop=True)
        )


class TaskDataset(Dataset):
    """
    A class that converts pd.DataFrame to Dataset and works with it.
    """

    def __init__(self, data: pd.DataFrame) -> None:
        """
        Initialize an instance of TaskDataset.

        Args:
            data (pandas.DataFrame): Original data
        """
        self._data = data

    def __len__(self) -> int:
        """
        Return the number of items in the dataset.

        Returns:
            int: The number of items in the dataset
        """
        return len(self._data)

    def __getitem__(self, index: int) -> tuple[str, ...]:
        """
        Retrieve an item from the dataset by index.

        Args:
            index (int): Index of sample in dataset

        Returns:
            tuple[str, ...]: The item to be received
        """
        return (self._data.iloc[index][ColumnNames.SOURCE.name], )


    @property
    def data(self) -> DataFrame:
        """
        Property with access to preprocessed DataFrame.

        Returns:
            pandas.DataFrame: Preprocessed DataFrame
        """
        return self._data

class LLMPipeline(AbstractLLMPipeline):
    """
    A class that initializes a model, analyzes its properties and infers it.
    """

    def __init__(
        self, model_name: str, dataset: TaskDataset, max_length: int, batch_size: int, device: str
    ) -> None:
        """
        Initialize an instance of LLMPipeline.

        Args:
            model_name (str): The name of the pre-trained model
            dataset (TaskDataset): The dataset used
            max_length (int): The maximum length of generated sequence
            batch_size (int): The size of the batch inside DataLoader
            device (str): The device for inference
        """
        super().__init__(model_name, dataset, max_length, batch_size, device)

        self._tokenizer = T5TokenizerFast.from_pretrained(self._model_name)
        self._model = AutoModelForSeq2SeqLM.from_pretrained(self._model_name).to(self._device)


    def analyze_model(self) -> dict:
        """
        Analyze model computing properties.

        Returns:
            dict: Properties of a model
        """
        test_model = AutoModelForSeq2SeqLM.from_pretrained(self._model_name)

        vocab_size = test_model.config.vocab_size
        emb_size = test_model.config.hidden_size

        input_data = torch.ones((1, emb_size), dtype=torch.long)
        model_summary = summary(test_model,
                                input_data=input_data, decoder_input_ids=input_data)

        return {
            'input_shape': model_summary.summary_list[0].input_size,
            'embedding_size': emb_size,
            'output_shape': model_summary.summary_list[-1].output_size,
            'num_trainable_params': model_summary.trainable_params,
            'vocab_size': vocab_size,
            'size': model_summary.total_param_bytes,
            'max_context_length': test_model.config.max_length
        }


    @report_time
    def infer_sample(self, sample: tuple[str, ...]) -> str | None:
        """
        Infer model on a single sample.

        Args:
            sample (tuple[str, ...]): The given sample for inference with model

        Returns:
            str | None: A prediction
        """
        return self._infer_batch([sample])[0]


    @report_time
    def infer_dataset(self) -> pd.DataFrame:
        """
        Infer model on a whole dataset.

        Returns:
            pd.DataFrame: Data with predictions
        """
        predictions = []

        dataloader = DataLoader(self._dataset, batch_size=self._batch_size)
        for batch in dataloader:
            output = self._infer_batch(batch)

            predictions.extend(output)

        res = pd.DataFrame(
            {ColumnNames.TARGET.name: self._dataset.data[ColumnNames.TARGET.name],
             ColumnNames.PREDICTION.name: predictions}
        )
        return res


    @torch.no_grad()
    def _infer_batch(self, sample_batch: Sequence[tuple[str, ...]]) -> list[str]:
        """
        Infer model on a single batch.

        Args:
            sample_batch (Sequence[tuple[str, ...]]): Batch to infer the model

        Returns:
            list[str]: Model predictions as strings
        """
        model_input = self._tokenizer(sample_batch[0],
                                      return_tensors='pt',
                                      max_length=self._max_length,
                                      padding=True,
                                      truncation=True).to(self._device)

        self._model.eval()
        output = self._model.generate(**model_input)
        decoded = self._tokenizer.batch_decode(output, skip_special_tokens=True)

        return list(map(str, decoded))


class TaskEvaluator(AbstractTaskEvaluator):
    """
    A class that compares prediction quality using the specified metric.
    """

    def __init__(self, data_path: Path, metrics: Iterable[Metrics]) -> None:
        """
        Initialize an instance of Evaluator.

        Args:
            data_path (pathlib.Path): Path to predictions
            metrics (Iterable[Metrics]): List of metrics to check
        """
        super().__init__(metrics)
        self._loaded_metrics = [load(metric.value, seed=77) for metric in self._metrics]
        self._data_path = data_path


    @report_time
    def run(self) -> dict | None:
        """
        Evaluate the predictions against the references using the specified metric.

        Returns:
            dict | None: A dictionary containing information about the calculated metric
        """
        results_df = pd.read_csv(self._data_path)
        predictions = results_df[ColumnNames.PREDICTION.name]
        target = results_df[ColumnNames.TARGET.name]

        comparison = {}
        for metric in self._loaded_metrics:
            calculated = metric.compute(predictions=predictions, references=target)

            if metric.name == Metrics.ROUGE.value:
                comparison[metric.name] = float(calculated['rougeL'])
            else:
                comparison[metric.name] = float(calculated[metric.name])

        return comparison
