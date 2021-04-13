import requests
import os #
from tqdm import tqdm # tqdm
import platform
import shutil
import re
import argparse
import asyncio
import aiohttp
import subprocess
import io
from datetime import datetime, timedelta

import time
import av                                   # av
from lxml import etree                      # lxml
from lxml.etree import QName                # lxml
import lxml.html                            # lxml

s = requests.Session()

av.logging.set_level(av.logging.PANIC)

class Stream:
    def __init__(self, stream_type, bitrate, codec, quality, base_url):
        self.stream_type = stream_type
        self.bitrate = bitrate
        self.codec = codec
        self.quality = quality
        self.base_url = base_url

    def __str__(self):
        return f"{self.quality:{' '}{'>'}{9}} Bitrate: {self.bitrate:{' '}{'>'}{8}} Codec: {self.codec}"

# class Segment:
#     def __init__(self, stream, seg_num):
#         self.url = stream.base_url + str(seg_num)
#         self.seg_num = seg_num
#         self.data = io.BytesIO()
#         self.success = False


def get_mpd_data(video_url):
    req = s.get(video_url)
    if 'dashManifestUrl\\":\\"' in req.text:
        mpd_link = req.text.split('dashManifestUrl\\":\\"')[-1].split('\\"')[0].replace("\/", "/")
    elif 'dashManifestUrl":"' in req.text:
        mpd_link = req.text.split('dashManifestUrl":"')[-1].split('"')[0].replace("\/", "/")
    else:
        doc = lxml.html.fromstring(req.content)
        form = doc.xpath('//form[@action="https://consent.youtube.com/s"]')
        if len(form) > 0:
            print("Consent check detected. Will try to pass...")
            params = form[0].xpath('.//input[@type="hidden"]')
            pars = {}
            for par in params:
                pars[par.attrib['name']] = par.attrib['value']
            s.post("https://consent.youtube.com/s", data=pars)
            return get_mpd_data(video_url)
        return None
    return s.get(mpd_link).text


def process_mpd(mpd_data):
    tree = etree.parse(io.BytesIO(mpd_data.encode()))
    root = tree.getroot()
    nsmap = {(k or "def"): v for k, v in root.nsmap.items()}
    time = root.attrib[QName(nsmap["yt"], "mpdResponseTime")]
    d_time = datetime.strptime(time, "%Y-%m-%dT%H:%M:%S.%f")
    total_seg = (
        int(root.attrib[QName(nsmap["yt"], "earliestMediaSequence")])
        + len(tree.findall(".//def:S", nsmap))
        - 1
    )
    seg_len = int(float(root.attrib["minimumUpdatePeriod"][2:-1]))
    attribute_sets = tree.findall(".//def:Period/def:AdaptationSet", nsmap)
    v_streams = []
    a_streams = []
    for a in attribute_sets:
        stream_type = a.attrib["mimeType"][0]
        for r in a.findall(".//def:Representation", nsmap):
            bitrate = int(r.attrib["bandwidth"])
            codec = r.attrib["codecs"]
            base_url = r.find(".//def:BaseURL", nsmap).text + "sq/"
            if stream_type == "a":
                quality = r.attrib["audioSamplingRate"]
                a_streams.append(Stream(stream_type, bitrate, codec, quality, base_url))
            elif stream_type == "v":
                quality = f"{r.attrib['width']}x{r.attrib['height']}"
                v_streams.append(Stream(stream_type, bitrate, codec, quality, base_url))
    a_streams.sort(key=lambda x: x.bitrate, reverse=True)
    v_streams.sort(key=lambda x: x.bitrate, reverse=True)
    return a_streams, v_streams, total_seg, d_time, seg_len


async def fetch(session, url, i, pbar, sem):
    async with sem, session.get(url) as response:
        resp = await response.read()
        pbar.update()
        return resp

async def get_segments(total_segments, video_base, audio_base):
    if video_base and audio_base:
        total_seg = 2*len(total_segments)
    else:
        total_seg = len(total_segments)
    pbar = tqdm(total=total_seg, desc="Downloading segments")
    async with aiohttp.ClientSession() as session:
        sem = asyncio.Semaphore(12)
        tasks = []
        for i in total_segments:
            if video_base:
                tasks.append(asyncio.create_task(fetch(session, f"{video_base}{i}", i, pbar, sem)))
            if audio_base:
                tasks.append(asyncio.create_task(fetch(session, f"{audio_base}{i}", i, pbar, sem)))
        res = await asyncio.gather(*tasks)
    pbar.close()
    if video_base and not audio_base:
        video_file = io.BytesIO()
        for i in range(0, len(res)):
            video_file.write(res[i])
        
    if audio_base and not video_base:
        audio_file = io.BytesIO()
        for i in range(0, len(res)):
            audio_file.write(res[i])

    if video_base and audio_base:
        video_file = io.BytesIO()
        audio_file = io.BytesIO()
        for i in range(0, len(res), 2):
            video_file.write(res[i])
            audio_file.write(res[i+1])
    
    if video_base and audio_base: return video_file, audio_file
    if video_base and not audio_base: return video_file, None
    if audio_base and not video_base: return None, audio_file

def mux_to_file(output, aud, vid):
    if aud is None or vid is None:
        output = av.open(output, "w")
        if vid is not None and aud is None:
            vid.seek(0)
            video = av.open(vid, "r")
            v_in = video.streams.video[0]
            video_p = video.demux(v_in)
            output_video = output.add_stream(template=v_in)
            
            last_pts = 0
            for packet in video_p:
                if packet.dts is None:
                    continue
                
                packet.dts = last_pts
                packet.pts = last_pts
                last_pts += packet.duration

                packet.stream = output_video
                output.mux(packet)
            video.close()
        
        if aud is not None and vid is None:
            aud.seek(0)
            audio = av.open(aud, "r")
            a_in = audio.streams.audio[0]
            audio_p = audio.demux(a_in)
            output_audio = output.add_stream(template=a_in)

            last_pts = 0
            for packet in audio_p:
                if packet.dts is None:
                    continue

                packet.dts = last_pts
                packet.pts = last_pts
                last_pts += packet.duration

                packet.stream = output_audio
                output.mux(packet)
            audio.close()

        output.close()
    else:
        # seek 0: https://github.com/PyAV-Org/PyAV/issues/508#issuecomment-488710828
        vid.seek(0)
        aud.seek(0)
        video = av.open(vid, "r")
        audio = av.open(aud, "r")
        output = av.open(output, "w")
        v_in = video.streams.video[0]
        a_in = audio.streams.audio[0]

        video_p = video.demux(v_in)
        audio_p = audio.demux(a_in)

        output_video = output.add_stream(template=v_in)
        output_audio = output.add_stream(template=a_in)

        last_pts = 0
        for packet in video_p:
            if packet.dts is None:
                continue
            
            packet.dts = last_pts
            packet.pts = last_pts
            last_pts += packet.duration

            packet.stream = output_video
            output.mux(packet)

        last_pts = 0
        for packet in audio_p:
            if packet.dts is None:
                continue

            packet.dts = last_pts
            packet.pts = last_pts
            last_pts += packet.duration

            packet.stream = output_audio
            output.mux(packet)

        output.close()
        audio.close()
        video.close()

def info(a, v, m, s):
    print(f"You can go back {int(m*2/3600)} hours and {int(m*2%3600/60)} minutes...")
    print(f"Download avaliable from {(datetime.today() + timedelta(hours=2)) - timedelta(seconds=m*2)}")
    print("\nAudio stream ids")
    for i in range(len(a)):
        print(f"{i}:  {str(a[i])}")

    print("\nVideo stream ids")
    for i in range(len(v)):
        print(f"{i}:  {str(v[i])}")
    
    print("\nUse format -1 for no video or audio")

# def download_func(seg):
#     while True:
#         req = s.get(seg.url)
#         if req.status_code == 200:
#             break
#         time.sleep(1)
#     return req.content

# def download(stream, seg_range, threads=1):
#     segments = []
#     for seg in seg_range:
#         segments.append(Segment(stream, seg))

#     results = ThreadPool(threads).imap(download_func, segments)
#     combined_file = io.BytesIO()
#     segs_downloaded = 0
#     for res in tqdm(results, total=len(segments), unit="seg"):
#         combined_file.write(res)

#     return combined_file

def parse_datetime(inp, utc=True):
    formats = ["%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M", "%d.%m %H:%M", "%H:%M", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m %H:%M:%S", "%H:%M:%S"]
    for fmt in formats:
        try:
            d_time = datetime.strptime(inp, fmt)
            today = datetime.today()
            if not ('d' in fmt):
                d_time = d_time.replace(year=today.year, month=today.month,day=today.day)
            if not ('Y' in fmt):
                d_time = d_time.replace(year=today.year)
            if utc:
                return d_time
            return d_time - timedelta(hours=2)
        except ValueError:
            pass
    return -1

def parse_duration(inp):
    x = re.findall("([0-9]+[hmsHMS])", inp)
    if len(x) == 0:
        try:
            number = int(inp)
        except:
            return -1
        return number
    else:
        total_seconds = 0
        for chunk in x:
            if chunk[-1] == "h":
                total_seconds += int(chunk[:-1]) * 3600
            elif chunk[-1] == "m":
                total_seconds += int(chunk[:-1]) * 60
            elif chunk[-1] == "s":
                total_seconds += int(chunk[:-1])
        return total_seconds

def main(ffmpeg_executable, ffprobe_executable):
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output', metavar='OUTPUT_FILE', action='store', help='The output filename')
    parser.add_argument('-s', '--start', metavar='START_TIME', action='store', help='The start time (possible formats = "12:34", "12:34:56", "7.8.2009 12:34:56", "2009-08-07T12:34:56")')
    parser.add_argument('-e', '--end', metavar='END_TIME', action='store', help='The end time (same format as start time)')
    parser.add_argument('-d', '--duration', action='store', help='The duration (possible formats = "12h34m56s", "12m34s", "123s", "123m", "123h", ...)')
    parser.add_argument('-u', '--utc', action='store_true', help='Use UTC instead of local time for start and end time', default=False)
    parser.add_argument('-l', '--list-formats', action='store_true', help='List info about stream ids', default=False)
    parser.add_argument('-af', action='store', help='Select audio stream id', type=int, default=0)
    parser.add_argument('-vf', action='store', help='Select video stream id', type=int, default=0)
    parser.add_argument('-y', '--overwrite', action='store_true', help='Overwrite file without asking', default=False)
    parser.add_argument('url', metavar='URL', action='store', help='The URL of the YouTube stream')
    args = parser.parse_args()
    url = args.url
    output_path = args.output

    if output_path:
        formats = (".mp4", ".mkv", ".aac")
        if not output_path.endswith(formats):
            print("Error: Unsupported output file format!")
            print("Supported file formats are:")
            for f in formats:
                print(f"\t{f}")
            exit(1)

    mpd_data = get_mpd_data(url)
    if mpd_data is None:
        print("Error: Couldn't get MPD data!")
        return 0
    a, v, m, s, l = process_mpd(mpd_data)

    if args.list_formats == True:
        info(a, v, m, s)
        return 0

    if args.vf == -1:
        video_url = ""
    else:
        video_url = v[args.vf].base_url
    if args.af == -1:
        audio_url = ""
    else:
        audio_url = a[args.af].base_url

    start_time = (
        s - timedelta(seconds=m * l)
        if args.start == None
        else parse_datetime(args.start, args.utc)
    )

    if start_time == -1:
        print("Error: Couldn't parse start date!")
        exit(1)

    if args.duration == None and args.end == None:
        duration = m * l
    else:
        if args.duration == None:
            e_dtime = parse_datetime(args.end, args.utc)
            s_dtime = s if args.start == None else parse_datetime(args.start, args.utc)
            duration = (e_dtime - s_dtime).total_seconds()
        else:
            duration =  parse_duration(args.duration)

    if duration == -1:
        print("Error: Couldn't parse duration or end date!")
        exit(1)

    start_segment = m - round((s - start_time).total_seconds() / l)
    if start_segment < 0:
        start_segment = 0

    end_segment = start_segment + round(duration / l)
    if end_segment > m:
        print("Error: You are requesting segments that dont exist yet!")
        exit(1)
    total_segments = range(start_segment, end_segment)

    if not output_path:
        print(f"You can go back {int(m*2/3600)} hours and {int(m*2%3600/60)} minutes...")
        print(f"Download avaliable from {(datetime.today() + timedelta(hours=2)) - timedelta(seconds=m*2)}")
        exit(0)

    if os.path.exists(output_path):
        if args.overwrite:
            os.remove(output_path)
        else:
            while True:
                print(f'File "{output_path}" already exists! Overwrite? [y/N] ', end='')
                yn = input().lower()
                if yn == '' or yn == 'n':
                    exit(0)
                else:
                    os.remove(output_path)
                    break

    # get video and audio segments asynchronously
    video, audio = asyncio.get_event_loop().run_until_complete(get_segments(total_segments, video_url, audio_url))
    # video = download(v[0], total_segments, 4)
    # audio = download(a[0], total_segments, 4)
    print("Muxing into videofile...")
    mux_to_file(output_path, audio, video)
    print("\nVideo bylo uspesne stazeno, vyckejte jeste na dokonceni kopirovani na vas Google Disk.")

if __name__ == "__main__":
    plt = platform.system()
    if plt == "Windows":
        if not (os.path.exists("./bin/ffmpeg.exe") or shutil.which("ffmpeg") or os.path.exists("./bin/ffprobeexe") or shutil.which("ffprobe")):
            print("Run 'python download.py' first!")
            exit(1)
        elif os.path.exists("./bin/ffmpeg.exe") and os.path.exists("./bin/ffprobe.exe"):
            main(".\\bin\\ffmpeg.exe", ".\\bin\\ffprobe.exe")
        else:
            main("ffmpeg", "ffprobe")
    elif plt == "Linux" or plt == "Darwin":
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            print("Install ffmpeg to path!")
            exit(1)
        else:
            main("ffmpeg", "ffprobe")
