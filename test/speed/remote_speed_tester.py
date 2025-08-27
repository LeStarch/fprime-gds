#!/usr/bin/env python3
""" Speed test script """
import time
import struct
import fprime_gds.common.communication.ground

from pathlib import Path
from fprime_gds.executables.cli import ParserBase, MiddleWareParser, StandardPipelineParser
from fprime_gds.common.handlers import DataHandler
from fprime_gds.common.pipeline.standard import StandardPipeline


from fprime_gds.common.zmq_transport import ZmqGround

CHANNELS_PER_ITERATIONS = 100


class SpeedTesterParser(ParserBase):
    def get_arguments(self):
        """Arguments for soak monitoring"""
        return {
            ("--data-file",): {
                "action": "store",
                "required": True,
                "type": Path,
                "help": "Path to data to read N' spew",
            },
            ("--iterations",): {
                "type": int,
                "default": 10000,
                "help": "Number of iterations to run",
            }
        }

    def handle_arguments(self, args, **kwargs):
        """Handle arguments as parsed"""
        # Validate logs directory exists
        if not args.data_file.exists():
            raise ValueError(f"--data-file {args.data_file} does not exist")
        return args

def fixed_speed_test(data, pipeline):
    speed = 400000 # 400 KB/s
    iterations = 0
    once_every = len(data) / speed
    try:
        while True:
            last_time = time.perf_counter()
            iterations += 1
            pipeline.distributor.on_recv(data)
            now = time.perf_counter()
            if now - last_time > once_every:
                print(f"[WARNING] Slip of {now - last_time - once_every} seconds")
            # Busy loop until next time to send
            while now - last_time < once_every:
                now = time.perf_counter()
    except KeyboardInterrupt:
        pass
    return iterations

def iteration_test(data, ground, max_iterations=0):
    data_size = 0
    iterations = 0
    try:
        while True:
            iterations += 1
            data_size += sum([len(item) + 4 for item in data])
            ground.send_all(data)
            if iterations == max_iterations:
                break
    except KeyboardInterrupt:
        pass
    return data_size, iterations


def deframe_data(all_bytes):
    frames = []
    while len(all_bytes) > 0:
        size_bytes, = struct.unpack(">I", all_bytes[:4])
        all_bytes = all_bytes[4:]
        frames.append(all_bytes[:size_bytes])
        all_bytes = all_bytes[size_bytes:]
    return frames



def main():
    args, _ = ParserBase.parse_args([MiddleWareParser, SpeedTesterParser], client=True)

    # Set up a connection
    if args.zmq:
        print("[INFO] ZeroMQ ground")
        ground = ZmqGround(args.zmq_transport)
    else:
        print("[INFO] TTS ground")
        ground = fprime_gds.common.communication.ground.TCPGround(
            args.tts_addr, args.tts_port
        )
    ground.open()
    if args.zmq:
        ground.zmq.connect_outgoing()
    

    with open(args.data_file, "rb") as f:
        data = f.read()

    #frames = deframe_data(data)
    _ = input("[ENTER] To begin")
    start = time.perf_counter()
    try:
        # iterations = fixed_speed_test(data, pipeline)
        data_size,iterations = iteration_test([data], ground, args.iterations)
    except KeyboardInterrupt:
        pass
    assert iterations == args.iterations, "Iteration missmatch"
    print(f"[DATA SIZE] {data_size}")
    delta = time.perf_counter() - start
    channel_count = CHANNELS_PER_ITERATIONS*args.iterations
    print(f"[{delta:0.3f}] Channel Count: {channel_count} ({channel_count/delta:0.3f} ch/S) Bandwidth: {len(data) * args.iterations/delta/1024/1024:0.3f} MiB/S ")

if __name__ == '__main__':
    main() 
