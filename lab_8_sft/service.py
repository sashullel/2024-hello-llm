"""
Web service for model inference.
"""
# pylint: disable=too-few-public-methods, undefined-variable, unused-import, assignment-from-no-return, duplicate-code
try:
    from fastapi import FastAPI
except ImportError:
    print('Library "fastapi" not installed. Failed to import.')
    FastAPI = None

from lab_8_sft.main import LLMPipeline, TaskDataset

import logging
from dataclasses import dataclass

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config.constants import PROJECT_ROOT
from config.lab_settings import LabSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class Query:
    """
    Class representing an input text to be summarized by the model
    and the model (base/finetuned) to be used
    """
    question: str
    use_base_model: bool


def init_application() -> tuple[FastAPI, LLMPipeline, LLMPipeline]:
    """
    Initialize core application.

    Run: uvicorn lab_8_sft.service:app --reload

    Returns:
        tuple[fastapi.FastAPI, LLMPipeline, LLMPipeline]: instance of server and pipeline
    """
    max_length = 120
    batch_size = 1
    device = 'cpu'

    settings_path = PROJECT_ROOT / 'lab_8_sft' / 'settings.json'
    parameters = LabSettings(settings_path).parameters

    pretrained_pipeline = LLMPipeline(
        parameters.model, TaskDataset(pd.DataFrame()),
        max_length, batch_size, device
    )

    finetuned_model_dir = f'finetuned_{parameters.model.split("/")[-1]}'
    finetuned_model_path = PROJECT_ROOT / 'lab_8_sft' / 'dist' / finetuned_model_dir

    finetuned_pipeline = LLMPipeline(
        str(finetuned_model_path), TaskDataset(pd.DataFrame()),
        max_length, batch_size, device
    )

    classfication_app = FastAPI()

    return classfication_app, pretrained_pipeline, finetuned_pipeline


app, pre_trained_pipeline, fine_tuned_pipeline = init_application()


app_path = PROJECT_ROOT / 'lab_8_sft' / 'assets'
app.mount('/assets', StaticFiles(directory=app_path), name='assets')

logger.info('fastapi application started')


@app.get('/', response_class=HTMLResponse)
async def root(request: Request) -> HTMLResponse:
    """
    Serve the root endpoint of the service by rendering the main HTML template.

    Args:
        request (Request): The incoming HTTP request object, provided by FastAPI

    Returns:
        TemplateResponse: An HTMLResponse object containing the rendered `index.html` template
    """
    templates = Jinja2Templates(directory=app_path)
    return templates.TemplateResponse('index.html', {'request': request})


@app.post('/infer')
async def infer(request: Query) -> dict[str, str]:
    """
    Create an endpoint for model call.

    Args:
        request (Query): The incoming HTTP request object containing the input text

    Returns:
        dict[str, str]: A dictionary containing the inference results
    """
    logger.info('received request: %s', request.question)
    if Query.use_base_model:
        result = pre_trained_pipeline.infer_sample((request.question, ))
    else:
        result = fine_tuned_pipeline.infer_sample((request.question, ))
    logger.info('model inference complete: %s', result)

    return {'infer': result}
