"Implements the analysis, along with a REST API to upload and analyze a video."

import base64
import json
import logging
import os
import sys
import concurrent.futures
import numpy as np
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping, TypedDict, List
from pytube import YouTube
from vimeo_downloader import Vimeo
import cv2
from flask import Flask, Response, request
from flask_cors import CORS
from numpy import uint8
from numpy.typing import NDArray
from PIL import Image
from ultralytics import YOLO  # type: ignore
from werkzeug.datastructures import FileStorage

from . import auth, frameselector, db




def create_app(test_config = None) -> Flask:
    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    CORS(app)
    app.config.from_mapping(
        SECRET_KEY="dev",
        DATABASE=os.path.join(app.instance_path, "flaskr.sqlite"),
    )

    if test_config is None:
        # load the instance config, if it exists, when not testing
        app.config.from_pyfile("config.py", silent=True)
    else:
        # load the test config if passed in
        app.config.from_mapping(test_config)

    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    app.register_blueprint(auth.bp)
    db.init_app(app)

    @app.route("/upload", methods=["POST"])
    def upload() -> Response:
        "Receives an uploaded video to be analyzed."
        frameDict = []
        fps = 59.97
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        if request.files is None or "video" not in request.files:
            uploaded_video = VideoURL(
                request.form["video"],
                request.form["resolution"],
                request.form["frameRate"],
                request.form["model"],
                request.form["frameselector"],
            )
            selectors = uploaded_video.frameselector.split(", ")
            if 'youtube' in uploaded_video.file:
                yt = YouTube(uploaded_video.file)
                stream = yt.streams.filter(file_extension="mp4", res=480).first()
                frameDict = frameselector.YoutubeSelector().select_frames(stream, selectors)
                fps = stream.fps
            elif 'vimeo' in uploaded_video.file:
                v = Vimeo(uploaded_video.file)
                stream = v.streams[0]
                fps = frameselector.VimeoSelector().get_fps(stream)
                frameDict = frameselector.VimeoSelector().select_frames(stream, selectors)
            elif 'tiktok' in uploaded_video.file:
                frameDict = frameselector.TiktokSelector().select_frames(
                    uploaded_video.file, selectors)
        else:
            uploaded_video = VideoFile(
                request.files["video"],
                request.form["resolution"],
                request.form["frameRate"],
                request.form["model"],
                request.form["frameselector"],
            )
            selectors = uploaded_video.frameselector.split(", ")
            frameDict = frameselector.StructuralSimilaritySelector().select_frames(
                uploaded_video.file, selectors
            )

        selector_result = []
        for frame in frameDict:
            frames = frame["frames"]
            analysis_results = [
                analyze_frame(convert_frame_to_bin(frame.image)) for frame in frames
            ]
            response: list[AnalysisResponse] = [
                {
                    "frame_number": frame.frame_number,
                    "results": analysed.results,
                    "image": analysed.image,
                }
                for analysed, frame in zip(analysis_results, frames)
            ]
            selector_result.append(SelectorAnalysisResponse({
                "selector": frame["selector"],
                "frames": response,
                "run_time" : frame["run_time"]
            }))
        toReturn = {
            "results": selector_result,
            "fps" : fps,
        }
        return Response(json.dumps(toReturn), mimetype="application/json")

    @app.route("/uploadLive", methods=["POST"])
    def upload_live() -> Response:
        "Receives a live stream of video data to be analyzed."
        uploaded_file = request.form.getlist("files")
        selectors = request.form["frameselector"]
        frameDict = []
        for selector in selectors:
            if selector == 'Structural Similarity':
                frameDict.append(FrameResponse({
                    "selector": selector,
                    "frames": frameselector.LiveSelector().select_frames(uploaded_file, selector)
                })    
                )
            elif selector == 'Structural Similarity + Homogeny':
                frameDict.append(FrameResponse({
                    "selector": selector,
                    "frames": frameselector.LiveSelector().select_frames(uploaded_file, selector)
                })    
                )

        selector_result = []
        for frame in frameDict:
            frames = frame["frames"]
            analysis_results = [
                analyze_frame(convert_frame_to_bin(frame.image)) for frame in frames
            ]
            response: list[AnalysisResponse] = [
                {
                    "frame_number": frame.frame_number,
                    "results": analysed.results,
                    "image": analysed.image,
                }
                for analysed, frame in zip(analysis_results, frames)
            ]
            selector_result.append(SelectorAnalysisResponse({
                "selector": frame["selector"],
                "frames": response,
                "run_time" : frame["run_time"]
            }))
        return Response(json.dumps(selector_result), mimetype="application/json")
    
    

    return app


@dataclass
class VideoFile:
    "An uploaded video"
    file: FileStorage
    resolution: str
    frameRate: str
    model: str
    frameselector: str


@dataclass
class VideoURL:
    "An uploaded video"
    file: str
    resolution: str
    frameRate: str
    model: str
    frameselector: str


class ModelResult(TypedDict):
    class_id: str
    conf: float


class AnalysisResponse(TypedDict):
    frame_number: int | None
    results: list[ModelResult]
    image: str

@dataclass
class SelectedFrame:
    "The metadata of a selected frame."
    frame_number: int | None
    image: NDArray[np.uint8]

class FrameResponse(TypedDict):
    selector : str
    frames : list[SelectedFrame]
    run_time : float

class SelectorAnalysisResponse(TypedDict):
    selector: str
    frames: list[AnalysisResponse]
    run_time: float

def convert_frame_to_bin(frame: NDArray[uint8]) -> str:
    "Returns the data of JPEG file in base-64."
    _, imdata = cv2.imencode(".jpg", frame)
    return json.dumps({"image": base64.b64encode(imdata).decode("ascii")})  # type: ignore


@dataclass
class AnalysisResult:
    results: list[ModelResult]
    image: str


def analyze_frame(frame: str) -> AnalysisResult:
    "Uses the YOLOv8 model to detect objects in a base-64 encoded frame."
    try:
        model = analyze_frame.model
    except AttributeError:
        analyze_frame.model = YOLO("yolov8n.pt")
        model = analyze_frame.model
    load = json.loads(frame)
    imdata = base64.b64decode(load["image"])
    im = Image.open(BytesIO(imdata))
    results = model(im, stream=False, device="mps")  # type: ignore
    list_of_results: list[ModelResult] = []
    boxed_image = imdata
    if results:
        pil_img = Image.fromarray(results[0].plot())  # type: ignore
        buff = BytesIO()
        pil_img.save(buff, format="JPEG")
        boxed_image = base64.b64encode(buff.getvalue()).decode("utf-8")
        for result in results:  # type: ignore
            for box in result.boxes:  # type: ignore
                logging.info(result.names[box.cls[0].item()])  # type: ignore
                logging.info(box.conf[0].item())  # type: ignore
                data: ModelResult = {
                    "class_id": result.names[box.cls[0].item()],  # type: ignore
                    "conf": round(box.conf[0].item(), 2),  # type: ignore
                }
                list_of_results.append(data)
    logging.info(list_of_results)
    if isinstance(boxed_image, bytes):
        boxed_image = ""
    return AnalysisResult(list_of_results, boxed_image)
