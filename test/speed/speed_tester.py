#!/usr/bin/env python3
""" Speed test script """
import time
from pathlib import Path
from fprime_gds.executables.cli import ParserBase, StandardPipelineParser
from fprime_gds.common.handlers import DataHandler
from fprime_gds.common.pipeline.standard import StandardPipeline


DATA_SIZE = 0
CHANNELS_PER_ITERATIONS = 100

class NullHistory(DataHandler):
    """A history that does nothing."""
    def retrieve(self, start=None):
        pass

    def retrieve_new(self):
        pass

    def clear(self, start=None):
        pass

    def size(self):
        pass

    def data_callback(self, *args, **kwargs):
        pass

class ChannelCollector(DataHandler):
    """ Channel consumer that collects channel data """
    def __init__(self, iterations):
        self.data_size = 0
        self.channel_count = 0
        self.iterations = iterations
        self.done = False

    def data_callback(self, channel_data, sender=None):
        """Handle decoded channel data"""
        if self.channel_count == 0:
            self.start = time.perf_counter()
            self.channel_count += 1
            return
        
        self.data_size += DATA_SIZE / CHANNELS_PER_ITERATIONS
        self.channel_count += 1
        if self.channel_count >= (CHANNELS_PER_ITERATIONS * self.iterations):
            self.end()

    def is_done(self):
        return self.done

    def end(self):
        """ End the test """
        delta = time.perf_counter() - self.start
        print(f"[{delta:0.3f}] Channel Count: {self.channel_count} ({self.channel_count/delta:0.3f} ch/S) Bandwidth: {self.data_size/delta/1024/1024:0.3f} MiB/S ")
        self.done = True


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
            ("--remote",): {
                "action": "store_true",
                "default": False,
                "help": "Are we operating in a remote capacity",
            },
            ("--iterations",): {
                "type": int,
                "default": 10000,
                "help": "Number of iterations to run",
            },
        }

    def handle_arguments(self, args, **kwargs):
        """Handle arguments as parsed"""
        # Validate logs directory exists
        if not args.remote and (args.data_file is None or not args.data_file.exists()):
            raise ValueError(f"--data-file {args.data_file} does not exist")
        return args

def iteration_test(data, pipeline, max_iterations=0):
    iterations = 0
    try:
        while True:
            iterations += 1
            pipeline.distributor.on_recv(data)
            if iterations == max_iterations:
                break
    except KeyboardInterrupt:
        pass
    return iterations

def main():
    global DATA_SIZE
    args, _ = ParserBase.parse_args([StandardPipelineParser, SpeedTesterParser])
    pipeline = StandardPipeline()
    pipeline.histories.implementation = None
    pipeline = StandardPipelineParser.pipeline_factory(args, pipeline)

    # Disconnect if not running in remote mode
    if not args.remote:
        pipeline.disconnect()
    
    # Register a consumer
    channel_collector = ChannelCollector(args.iterations)
    pipeline.coders.register_channel_consumer(channel_collector)

    with open(args.data_file, "rb") as f:
        data = f.read()
        DATA_SIZE = len(data)
    try:
        # iterations = fixed_speed_test(data, pipeline)
        if args.remote:
            while not channel_collector.is_done():
                pass
        else:
            iterations = iteration_test(data, pipeline, args.iterations)
    except KeyboardInterrupt:
        pass
    pipeline.disconnect()
    channel_collector.end()


if __name__ == '__main__':
    main() 
