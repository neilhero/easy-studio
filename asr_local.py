#!/usr/bin/env python3
"""可选Whisper执行端：只读已安装本地模型，不下载模型，不连接外部服务。"""
import argparse
import json
import pathlib
import importlib.metadata


def main():
    p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--audio',required=True);a=p.parse_args()
    model=pathlib.Path(a.model)
    if not all((model/name).is_file() for name in ('model.bin','config.json','tokenizer.json')):raise ValueError('本地Whisper模型文件不完整')
    from faster_whisper import WhisperModel
    engine=WhisperModel(str(model),device='cpu',compute_type='int8',cpu_threads=3,local_files_only=True)
    segments,info=engine.transcribe(a.audio,language='zh',word_timestamps=True,vad_filter=True,beam_size=5)
    result=[{'start':s.start,'end':s.end,'text':s.text,'words':[{'start':w.start,'end':w.end,'word':w.word,'probability':w.probability} for w in s.words or []]} for s in segments]
    print(json.dumps({'model':str(model),'engine':'faster-whisper','version':importlib.metadata.version('faster-whisper'),'duration':info.duration,'segments':result},ensure_ascii=False))

if __name__=='__main__':main()
