import json
import logging
from argparse import ArgumentParser
from dataclasses import dataclass
from pathlib import Path

from models.time_interval import TimeInterval
from utils.clipping_utils import clip_section
from utils.config import log_flags
from utils.file_utils import create_temp_directory, execute_if_not_exist, assert_file_exists
from utils.probing_utils import get_video_duration, get_video_codec, get_video_resolution
from utils.smart_merge import smart_merge
from utils.subprocess_utils import run_subprocess


@dataclass
class RawConfig:
    directory: Path = Path(".")
    ignore_video_length: float = 0
    add_danmaku: bool = False
    # TODO: need a separate object for danmaku configuration; otherwise this is too long of a list
    xml_file: Path = Path("danmaku.xml")
    ass_file: Path = Path("danmaku.ass")
    merged_video: Path = Path("merged.flv")
    video_with_danmaku: Path = Path("video_with_danmaku.flv")
    codec: str = None
    force_resolution: bool = False
    resolution: str = "1920x1080"
    temp_dir: Path = Path("temp")
    smart_merge: bool = False
    split: bool = True
    initial_segment_length: int = 14400
    segment_length: int = 3600
    segment_extra: int = 3
    danmaku_offset: float = -3


class Config(RawConfig):
    def __init__(self, raw: RawConfig = RawConfig()):
        for attr in raw.__dict__.keys():
            setattr(self, attr, getattr(raw, attr))
        d = self.directory
        self.xml_file = d.joinpath(self.xml_file)
        self.ass_file = d.joinpath(self.ass_file)
        self.merged_video = d.joinpath(self.merged_video)
        self.video_with_danmaku = d.joinpath(self.video_with_danmaku)
        self.final_video: Path = self.video_with_danmaku if self.add_danmaku else self.merged_video
        self.temp_dir: Path = d.joinpath(self.temp_dir)


raw_config = RawConfig()
config = Config(raw_config)


def update_final_video():
    config.final_video = config.video_with_danmaku if config.add_danmaku else config.merged_video


def create_xml_danmaku():
    raise NotImplementedError("Please use BililiveRecorder to merge all xml files into a single danmaku.xml file.")


def create_ass_danmaku(resolution: str = "1920x1080"):
    success = execute_if_not_exist(config.xml_file, create_xml_danmaku)
    if not success:
        logging.warning("Cannot create xml danmaku file.")
        return
    temp_ass = config.temp_dir.joinpath("temp_danmaku.ass")
    run_subprocess(["python3", "utils/danmaku2ass.py", "-o", temp_ass, "-s", resolution,
                    "-f", "Bilibili", "-fn", "Microsoft YaHei", "-fs", "64",
                    "-a", "0.7", "-dm", "10", "-ds", "8", config.xml_file])
    # TODO: offset should be based on danmaku length: the longer the danmaku, the earlier it should have appeared
    run_subprocess(["ffmpeg", *log_flags, "-itsoffset", config.danmaku_offset,
                    "-i", temp_ass, "-c", "copy", config.ass_file])


def merge_videos():
    files = list(config.directory.glob("*.flv"))
    logging.info(f"Found {len(files)} flv files.")
    assert len(files) > 0, "No flv file found."
    logging.info(f"Excluding files less than {config.ignore_video_length} seconds long.")
    files = [f for f in files if get_video_duration(f) >= config.ignore_video_length]
    logging.info(f"{len(files)} flv files left after filtering according to duration.")
    file_list = config.temp_dir.joinpath(Path("files.txt"))
    smart_merge(list(files), config.temp_dir, file_list, smart=config.smart_merge)
    run_subprocess(["ffmpeg", "-loglevel", "error",
                    "-f", "concat", "-safe", "0",
                    "-i", file_list, "-c", "copy",
                    config.merged_video], echo=True)


BACK_SLASH = "\\"


def add_danmaku_to_video():
    assert_file_exists(config.merged_video)
    danmaku_params = {}
    if config.force_resolution:
        danmaku_params['resolution'] = config.resolution
    success = execute_if_not_exist(config.ass_file, create_ass_danmaku, **danmaku_params)
    if not success:
        raise FileNotFoundError(
            "ERROR: No danmaku file found. Change config.json if you don't want danmaku in the video.")
    filters = ["fps=60", f'ass={str(config.ass_file).replace(BACK_SLASH, "/")}']
    if config.force_resolution:
        resolution = config.resolution.split("x")
        width, height = int(resolution[0]), int(resolution[1])
        filters.insert(0, f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease")
        filters.insert(1, f"pad={width}:{height}:-1:-1:color=black")
    codec = config.codec if config.codec else get_video_codec(config.merged_video)
    logging.info("Burning ass subtitles into video. This may take a while.")
    run_subprocess(["ffmpeg", *log_flags, "-i", config.merged_video,
                    # backslash doesn't work for the ass filter, even on Windows machines
                    "-vf", ",".join(filters),
                    "-vcodec", codec, "-acodec", "copy",
                    config.video_with_danmaku], echo=True)


def create_final_video():
    execute_if_not_exist(config.merged_video, merge_videos, exit_if_fail=True)
    if config.add_danmaku:
        add_danmaku_to_video()
    assert_file_exists(config.final_video)


def split_final_video():
    execute_if_not_exist(config.final_video, create_final_video)
    if not config.split:
        logging.info("Final video produced. ")
        return
    duration = get_video_duration(config.final_video)
    segment_start = 0
    next_start = config.initial_segment_length
    segment_end = config.initial_segment_length + config.segment_extra
    if segment_end >= duration:
        logging.info("Final video is short enough. No splitting needed.")
        return
    logging.info(f"Final video is {duration} seconds long. Splitting...")
    segment_number = 1
    while segment_start < duration:
        file = config.directory.joinpath(Path(f"part{segment_number}.flv"))
        clip_section(config.final_video, file, TimeInterval(segment_start, segment_end))
        segment_number += 1
        segment_start = next_start
        next_start = segment_start + config.segment_length
        segment_end = next_start + config.segment_extra
    logging.info(f"Video is split. {segment_number - 1} parts created.")


def read_config_file(file: Path, target):
    with open(file, "r") as f:
        c = json.loads(f.read())
    for attr in c:
        setattr(target, attr, c[attr])


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler())
    parser = ArgumentParser()
    parser.add_argument("dir", type=Path, default=None)
    args = parser.parse_args()
    read_config_file(Path("config.json"), target=raw_config)
    if args.dir:
        raw_config.directory = args.dir
    global config
    config = Config(raw_config)
    create_temp_directory(config.temp_dir)
    assert config.directory.exists() and config.directory.is_dir()
    split_final_video()


if __name__ == "__main__":
    main()
