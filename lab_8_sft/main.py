"""
Laboratory work.

Fine-tuning Large Language Models for a downstream task.
"""
# pylint: disable=too-few-public-methods, undefined-variable, duplicate-code, unused-argument, too-many-arguments
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import torch
from datasets import load_dataset
from evaluate import load
from pandas import DataFrame
from peft import get_peft_model, LoraConfig
from torch.utils.data import DataLoader, Dataset
from torchinfo import summary
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

from config.lab_settings import SFTParams
from core_utils.llm.llm_pipeline import AbstractLLMPipeline
from core_utils.llm.metrics import Metrics
from core_utils.llm.raw_data_importer import AbstractRawDataImporter
from core_utils.llm.raw_data_preprocessor import AbstractRawDataPreprocessor, ColumnNames
from core_utils.llm.sft_pipeline import AbstractSFTPipeline
from core_utils.llm.task_evaluator import AbstractTaskEvaluator
from core_utils.llm.time_decorator import report_time


class RawDataImporter(AbstractRawDataImporter):
    """
    Custom implementation of data importer.
    """

    @report_time
    def obtain(self) -> None:
        """
        Import dataset.
        """
        self._raw_data = load_dataset(self._hf_name, split='validation').to_pandas()


class RawDataPreprocessor(AbstractRawDataPreprocessor):
    """
    Custom implementation of data preprocessor.
    """

    def analyze(self) -> dict:
        """
        Analyze preprocessed dataset.

        Returns:
            dict: dataset key properties.
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
            .rename(columns={'text': str(ColumnNames.SOURCE), 'label': str(ColumnNames.TARGET)})
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
        return (self._data.iloc[index][str(ColumnNames.SOURCE)],)

    @property
    def data(self) -> DataFrame:
        """
        Property with access to preprocessed DataFrame.

        Returns:
            pandas.DataFrame: Preprocessed DataFrame
        """
        return self._data



def tokenize_sample(
    sample: pd.Series, tokenizer: AutoTokenizer, max_length: int
) -> dict[str, torch.Tensor]:
    """
    Tokenize sample.

    Args:
        sample (pandas.Series): sample from a dataset
        tokenizer (transformers.models.auto.tokenization_auto.AutoTokenizer): Tokenizer to tokenize
            original data
        max_length (int): max length of sequence

    Returns:
        dict[str, torch.Tensor]: Tokenized sample
    """
    tokenized = tokenizer(sample[str(ColumnNames.SOURCE)],
                          padding='max_length', truncation=True, max_length=max_length)

    return {
        'input_ids': tokenized['input_ids'],
        'attention_mask': tokenized['attention_mask'],
        'labels': sample[str(ColumnNames.TARGET)]
    }


class TokenizedTaskDataset(Dataset):
    """
    A class that converts pd.DataFrame to Dataset and works with it.
    """

    def __init__(self, data: pd.DataFrame, tokenizer: AutoTokenizer, max_length: int) -> None:
        """
        Initialize an instance of TaskDataset.

        Args:
            data (pandas.DataFrame): Original data
            tokenizer (transformers.models.auto.tokenization_auto.AutoTokenizer): Tokenizer to
                tokenize the dataset
            max_length (int): max length of a sequence
        """
        self._data = data.apply(lambda x: tokenize_sample(x, tokenizer, max_length), axis=1)


    def __len__(self) -> int:
        """
        Return the number of items in the dataset.

        Returns:
            int: The number of items in the dataset
        """
        return len(self._data)

    def __getitem__(self, index: int) -> dict[str, torch.Tensor]:
        """
        Retrieve an item from the dataset by index.

        Args:
            index (int): Index of sample in dataset

        Returns:
            dict[str, torch.Tensor]: An element from the dataset
        """
        return dict(self._data.iloc[index])


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
            model_name (str): The name of the pre-trained model.
            dataset (TaskDataset): The dataset to be used for translation.
            max_length (int): The maximum length of generated sequence.
            batch_size (int): The size of the batch inside DataLoader.
            device (str): The device for inference.
        """

        super().__init__(model_name, dataset, max_length, batch_size, device)

        self._tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self._model_name).to(self._device).eval()


    def analyze_model(self) -> dict:
        """
        Analyze model computing properties.

        Returns:
            dict: Properties of a model
        """
        emb_size = self._model.config.max_position_embeddings
        input_data = torch.ones((1, emb_size), dtype=torch.long)

        test_model = AutoModelForSequenceClassification.from_pretrained(self._model_name)
        model_summary = summary(test_model, input_data=input_data, verbose=0)

        return {
            'input_shape': model_summary.input_size,
            'embedding_size': emb_size,
            'output_shape': model_summary.summary_list[-1].output_size,
            'num_trainable_params': model_summary.trainable_params,
            'vocab_size': test_model.config.vocab_size,
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
            {str(ColumnNames.TARGET): self._dataset.data[str(ColumnNames.TARGET)],
             str(ColumnNames.PREDICTION): predictions}
        )
        return res



    @torch.no_grad()
    def _infer_batch(self, sample_batch: Sequence[tuple[str, ...]]) -> list[str]:
        """
        Infer single batch.

        Args:
            sample_batch (Sequence[tuple[str, ...]]): batch to infer the model

        Returns:
            list[str]: model predictions as strings
        """
        if self._model is None:
            return []

        model_input = self._tokenizer(sample_batch[0],
                                      return_tensors='pt',
                                      max_length=self._max_length,
                                      padding=True,
                                      truncation=True).to(self._device)

        logits = self._model(**model_input).logits
        return [self._model.config.id2label[i] for i in torch.argmax(logits, dim=1).tolist()]


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
        self._loaded_metrics = [load(metric.value) for metric in self._metrics]
        self._data_path = data_path

    def run(self) -> dict | None:
        """
        Evaluate the predictions against the references using the specified metric.

        Returns:
            dict | None: A dictionary containing information about the calculated metric
        """
        results_df = pd.read_csv(self._data_path)
        predictions = results_df[str(ColumnNames.PREDICTION)]
        target = results_df[str(ColumnNames.TARGET)]

        return {
            metric.name: metric.compute(predictions=predictions, references=target,
                                        average='micro')[metric.name]
            for metric in self._loaded_metrics
        }


class SFTPipeline(AbstractSFTPipeline):
    """
    A class that initializes a model, fine-tuning.
    """

    def __init__(self, model_name: str, dataset: Dataset, sft_params: SFTParams) -> None:
        """
        Initialize an instance of ClassificationSFTPipeline.

        Args:
            model_name (str): The name of the pre-trained model.
            dataset (torch.utils.data.dataset.Dataset): The dataset used.
            sft_params (SFTParams): Fine-Tuning parameters.
        """
        super().__init__(model_name, dataset)
        self._model = AutoModelForSequenceClassification.from_pretrained(self._model_name)
        self._lora_config = LoraConfig(r=4, lora_alpha=8, lora_dropout=0.1)
        self._device = sft_params.device
        self._peft_model = get_peft_model(self._model, self._lora_config).to(self._device)

        self._batch_size = sft_params.batch_size
        self._max_length = sft_params.max_length
        self._max_sft_steps = sft_params.max_fine_tuning_steps
        self._finetuned_model_path = sft_params.finetuned_model_path
        self._learning_rate = sft_params.learning_rate

    def run(self) -> None:
        """
        Fine-tune model.
        """
        if (self._model is None or self._finetuned_model_path is None or
                self._batch_size is None or self._learning_rate is None or
                self._max_sft_steps is None):
            return

        training_args = TrainingArguments(
            output_dir=self._finetuned_model_path,
            per_device_train_batch_size=self._batch_size,
            learning_rate=self._learning_rate,
            max_steps=self._max_sft_steps,
            save_strategy='no',
            use_cpu=(self._device == 'cpu'),
            load_best_model_at_end=False
        )

        trainer = Trainer(model=self._peft_model, args=training_args, train_dataset=self._dataset)
        trainer.train()

        merged_model = self._peft_model.merge_and_unload()
        merged_model.save_pretrained(self._finetuned_model_path)

        tokenizer = AutoTokenizer.from_pretrained(self._model_name)
        tokenizer.save_pretrained(self._finetuned_model_path)
