#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import dataclasses
import logging
import os
import pathlib
import signal
import sys
import tempfile
import typing

import holoscan
import jsonargparse
import matplotlib as mpl
from holohub import basic_network, rf_array
from holohub.rf_array.digital_metadata import DigitalMetadataSink
from holohub.rf_array.params import (
    DigitalRFSinkParams,
    NetConnectorBasicParams,
    ResamplePolyParams,
    RotatorScheduledParams,
    SubchannelSelectParams,
    add_chunk_kwargs,
)
from jsonargparse.typing import NonNegativeInt, PositiveInt
from spectrogram import (
    Spectrogram,
    SpectrogramMQTT,
    SpectrogramMQTTParams,
    SpectrogramOutput,
    SpectrogramOutputParams,
    SpectrogramParams,
)

mpl.use("agg")

# set up Holoscan logger
env_log_level = os.environ.get("HOLOSCAN_LOG_LEVEL", "WARN").upper()
log_level_map = {
    "OFF": "NOTSET",
    "CRITICAL": "CRITICAL",
    "ERROR": "ERROR",
    "WARN": "WARNING",
    "INFO": "INFO",
    "DEBUG": "DEBUG",
    "TRACE": "DEBUG",
}
log_level = log_level_map[env_log_level]
holoscan_handler = logging.StreamHandler()
holoscan_handler.setFormatter(
    logging.Formatter(
        fmt="[{levelname}] [{filename}:{lineno}] {message}",
        style="{",
    )
)
holoscan_handler.setLevel(log_level)
holoscan_logger = logging.getLogger("holoscan")
holoscan_logger.setLevel(log_level)
holoscan_logger.propagate = False
holoscan_logger.addHandler(holoscan_handler)

jsonargparse.set_parsing_settings(docstring_parse_attribute_docstrings=True)


DEFAULT_BATCH_SIZE = 3125
DEFAULT_MAX_PACKET_SIZE = 8256
DEFAULT_NUM_SUBCHANNELS = 1


@dataclasses.dataclass
class SchedulerParams:
    """Event-based scheduler parameters"""

    worker_thread_number: PositiveInt = 8
    """Number of worker threads"""
    stop_on_deadlock: bool = True
    """Whether the application will terminate if a deadlock occurs"""
    stop_on_deadlock_timeout: int = 500
    """Time (in ms) to wait before determining that a deadlock has occurred"""


@dataclasses.dataclass
class PipelineParams:
    """Pipeline configuration parameters"""

    selector: bool = False
    "Enable / disable subchannel selector"
    converter: bool = True
    "Enable / disable complex int to float converter"
    rotator: bool = True
    "Enable / disable frequency rotator"
    resampler0: bool = True
    "Enable / disable the first stage resampler"
    resampler1: bool = True
    "Enable / disable the second stage resampler"
    resampler2: bool = True
    "Enable / disable the third stage resampler"
    spec_after: str = "rotator"
    "Which operator form converter through resampler2 to place the spectrogram after"
    spec_resampler: bool = True
    "Enable / disable the zeroth stage pre-spectrogram resampler"
    spectrogram: bool = True
    "Enable / disable spectrogram processing"
    spectrogram_mqtt: bool = True
    "Enable / disable spectrogram output over MQTT"
    spectrogram_output: bool = True
    "Enable / disable spectrogram output to files"
    int_converter: bool = False
    "Enable / disable complex float to int converter"
    digital_rf: bool = True
    "Enable / disable writing output to Digital RF"
    metadata: bool = True
    "Enable / disable writing inherent and user-supplied Digital RF metadata"


@dataclasses.dataclass
class BasicNetworkOperatorParams:
    """Basic network operator parameters"""

    ip_addr: str = "0.0.0.0"
    """IP address to bind to"""
    dst_port: NonNegativeInt = 60134
    "UDP or TCP port to listen on"
    l4_proto: str = "udp"
    "Layer 4 protocol (udp or tcp)"
    batch_size: PositiveInt = DEFAULT_BATCH_SIZE
    "Number of packets in batch"
    max_payload_size: PositiveInt = DEFAULT_MAX_PACKET_SIZE
    "Maximum payload size expected from sender"


def build_channel_subparser(parser, ch):
    parser.add_argument(
        f"--{ch}.enabled", type=bool, default=True if ch == "channel0" else False
    )
    parser.add_argument(f"--{ch}.pipeline", type=PipelineParams)
    parser.add_argument(f"--{ch}.basic_network", type=BasicNetworkOperatorParams)
    parser.add_argument(
        f"--{ch}.packet",
        type=NetConnectorBasicParams,
        default=NetConnectorBasicParams(
            batch_size=DEFAULT_BATCH_SIZE,
            batch_capacity=10,
            max_packet_size=DEFAULT_MAX_PACKET_SIZE,
            num_subchannels=DEFAULT_NUM_SUBCHANNELS,
            num_samples=6400000,
            buffer_size=5,
            freq_idx_scaling=1000,
            freq_idx_offset=0,
            apply_conjugate=False,
            spoof_header=False,
            packet_skip_bytes=64,
            header_metadata={
                "start_sample_idx": 0,
                "sample_rate_numerator": 64000000,
                "sample_rate_denominator": 1,
                "freq_idx": 0,
                "num_subchannels": DEFAULT_NUM_SUBCHANNELS,
                "pkt_samples": 2048,
                "bits_per_int": 16,
                "is_complex": 1,
            },
        ),
    )
    parser.add_argument(
        f"--{ch}.selector",
        type=SubchannelSelectParams,
        default=SubchannelSelectParams(subchannel_idx=[0]),
    )
    parser.add_argument(
        f"--{ch}.rotator",
        type=RotatorScheduledParams,
        default=RotatorScheduledParams(
            cycle_duration_secs=1,
            cycle_start_timestamp=0,
            schedule=[{"start": 0, "freq": 31.65e6}],
        ),
    )
    parser.add_argument(
        f"--{ch}.resampler0",
        type=ResamplePolyParams,
        default=ResamplePolyParams(
            up=1,
            down=32,
            outrate_cutoff=1.0,
            # transition_width: 2 * (cutoff - 1 / remaining_dec)
            #                   2 * (1.0 - 1 / 20) = 1.9
            outrate_transition_width=1.9,
            # tweak attenuation to get desired number of taps indicated by
            # scipy.signal.kaiserord(attenuation_db, outrate_transition_width / down)
            attenuation_db=102.5,
        ),
    )
    parser.add_argument(
        f"--{ch}.resampler1",
        type=ResamplePolyParams,
        default=ResamplePolyParams(
            up=1,
            down=5,
            outrate_cutoff=1.0,
            # transition_width: 2 * (cutoff - 1 / remaining_dec)
            #                   2 * (1.0 - 1 / 4) = 1.5
            outrate_transition_width=1.5,
            # tweak attenuation to get desired number of taps indicated by
            # scipy.signal.kaiserord(attenuation_db, outrate_transition_width / down)
            attenuation_db=102,
        ),
    )
    parser.add_argument(
        f"--{ch}.resampler2",
        type=ResamplePolyParams,
        default=ResamplePolyParams(
            up=1,
            down=4,
            outrate_cutoff=1.0,
            outrate_transition_width=0.2,
            # tweak attenuation to get desired number of taps indicated by
            # scipy.signal.kaiserord(attenuation_db, outrate_transition_width / down)
            attenuation_db=99,
        ),
    )
    parser.add_argument(
        f"--{ch}.spec_resampler",
        type=ResamplePolyParams,
        default=ResamplePolyParams(
            up=1,
            down=2,
            outrate_cutoff=1.0,
            outrate_transition_width=0.4,
            # tweak attenuation to get desired number of taps indicated by
            # scipy.signal.kaiserord(attenuation_db, outrate_transition_width / down)
            attenuation_db=85,
        ),
    )
    parser.add_argument(
        f"--{ch}.spectrogram",
        type=SpectrogramParams,
        default=SpectrogramParams(
            window="hann",
            nperseg=1600,
            noverlap=None,
            nfft=None,
            detrend=False,
            reduce_op="max",
            num_spectra_per_chunk=1,
        ),
    )
    parser.add_argument(
        f"--{ch}.spectrogram_mqtt",
        type=SpectrogramMQTTParams,
        default=SpectrogramMQTTParams(
            input_buffer_capacity=100,
            service_name="recorder_fft_ch000",
            status_topic="dt/simone/{service_name}/{node_id}/status",
            data_topic="dt/simone/{service_name}/{node_id}/data",
            mqtt_host="localhost",
            mqtt_port=1883,
            mqtt_keepalive=60,
            payload_format="f32buffer",
        ),
    )
    parser.add_argument(
        f"--{ch}.spectrogram_output",
        type=SpectrogramOutputParams,
        default=SpectrogramOutputParams(
            num_spectra_per_output=600,
            figsize=[6.4, 4.8],
            dpi=200,
            col_wrap=1,
            cmap="viridis",
            snr_db_min=-5,
            snr_db_max=30,
            plot_subdir="spectrograms/ch000",
        ),
    )
    parser.add_argument(
        f"--{ch}.drf_sink",
        type=DigitalRFSinkParams,
        default=DigitalRFSinkParams(
            channel_dir="simone/ch000",
            subdir_cadence_secs=3600,
            file_cadence_millisecs=60000,
        ),
    )
    parser.add_argument(
        f"--{ch}.metadata",
        type=typing.Optional[dict[str, typing.Any]],
        default={
            "receiver": {
                "description": "Holoscan SIMONe recorder",
            },
            "processing": {
                "decimation": 640,
                "interpolation": 1,
            },
        },
    )

    # link non-operator arguments that we use from recorder_service
    parser.link_arguments(
        "ram_ringbuffer_path", f"{ch}.drf_sink.output_path", apply_on="parse"
    )
    parser.link_arguments(
        "output_path", f"{ch}.spectrogram_output.output_path", apply_on="parse"
    )


def build_config_parser():
    parser = jsonargparse.ArgumentParser(
        prog="simone_recorder",
        description="Process and record RF data for the SIMONe meteor radar network",
        default_env=True,
    )
    # special config argument to load from yaml file
    parser.add_argument("--config", action="config")
    # operator arguments
    parser.add_argument("--scheduler", type=SchedulerParams)

    # non-operator arguments that we use from recorder_service
    parser.add_argument(
        "--ram_ringbuffer_path", type=typing.Optional[os.PathLike], default="."
    )
    parser.add_argument("--output_path", type=typing.Optional[os.PathLike], default=".")

    # add channel-based arguments
    for ch_idx in range(2):
        build_channel_subparser(parser, f"channel{ch_idx}")

    return parser


class App(holoscan.core.Application):
    def add_spectrogram_flow(self, ch, cuda_stream_pool, op_dict):
        ch_kwargs = self.kwargs(ch)

        after_op_name = ch_kwargs["pipeline"].get("spec_after", "rotator")
        last_op, last_chunk_shape = op_dict[after_op_name]

        if ch_kwargs["pipeline"]["spectrogram"]:
            if ch_kwargs["pipeline"]["spec_resampler"]:
                resample_kwargs = add_chunk_kwargs(
                    last_chunk_shape, **ch_kwargs["spec_resampler"]
                )
                spec_resampler = rf_array.ResamplePoly(
                    self,
                    cuda_stream_pool,
                    name=f"{ch}_spec_resampler",
                    **resample_kwargs,
                )
                self.add_flow(last_op, spec_resampler)
                last_op = spec_resampler
                last_chunk_shape = (
                    last_chunk_shape[0]
                    * resample_kwargs["up"]
                    // resample_kwargs["down"],
                    last_chunk_shape[1],
                )

            spectrogram = Spectrogram(
                self,
                cuda_stream_pool,
                name=f"{ch}_spectrogram",
                **add_chunk_kwargs(last_chunk_shape, **ch_kwargs["spectrogram"]),
            )
            # Queue policy is currently set by specifying a connector in setup()
            # # drop old messages rather than get backed up by slow
            # # downstream operators
            # spectrogram.queue_policy(
            #     port_name="spec_out",
            #     port_type=holoscan.core.IOSpec.IOType.OUTPUT,
            #     policy=holoscan.core.IOSpec.QueuePolicy.POP,
            # )
            self.add_flow(last_op, spectrogram)

            if ch_kwargs["pipeline"]["spectrogram_mqtt"]:
                spec_mqtt_kwargs = ch_kwargs["spectrogram_mqtt"]
                spec_mqtt_kwargs.update(
                    spec_sample_cadence=spectrogram.spec_sample_cadence,
                )
                spectrogram_mqtt = SpectrogramMQTT(
                    self,
                    ## CudaStreamCondition doesn't work with a message queue size
                    ## larger than 1, so get by without it for now
                    # holoscan.conditions.MessageAvailableCondition(
                    #     self,
                    #     receiver="spec_in",
                    #     name=f"{ch}_spectrogram_mqtt_message_available",
                    # ),
                    # holoscan.conditions.CudaStreamCondition(
                    #     self, receiver="spec_in", name=f"{ch}_spectrogram_mqtt_stream_sync"
                    # ),
                    # # no downstream condition, and we don't want one
                    cuda_stream_pool,
                    name=f"{ch}_spectrogram_mqtt",
                    **spec_mqtt_kwargs,
                )
                self.add_flow(spectrogram, spectrogram_mqtt)

            if ch_kwargs["pipeline"]["spectrogram_output"]:
                spec_out_kwargs = ch_kwargs["spectrogram_output"]
                spec_out_kwargs.update(
                    nfft=spectrogram.nfft,
                    spec_sample_cadence=spectrogram.spec_sample_cadence,
                    num_subchannels=spectrogram.num_subchannels,
                    data_subdir=(f"{ch_kwargs['drf_sink']['channel_dir']}_spectrogram"),
                )
                spectrogram_output = SpectrogramOutput(
                    self,
                    ## CudaStreamCondition doesn't work with a message queue size
                    ## larger than 1, so get by without it for now
                    # holoscan.conditions.MessageAvailableCondition(
                    #     self,
                    #     receiver="spec_in",
                    #     name=f"{ch}_spectrogram_output_message_available",
                    # ),
                    # holoscan.conditions.CudaStreamCondition(
                    #     self, receiver="spec_in", name=f"{ch}_spectrogram_output_stream_sync"
                    # ),
                    # # no downstream condition, and we don't want one
                    cuda_stream_pool,
                    name=f"{ch}_spectrogram_output",
                    **spec_out_kwargs,
                )
                self.add_flow(spectrogram, spectrogram_output)

    def add_channel_flow(self, ch, cuda_stream_pool, priority_stream_pool):
        ch_kwargs = self.kwargs(ch)

        if not ch_kwargs["enabled"]:
            return

        basic_net_rx = basic_network.BasicNetworkOpRx(
            self,
            name=f"{ch}_basic_network_rx",
            **ch_kwargs["basic_network"],
        )
        basic_net_rx.spec.outputs["burst_out"].connector(
            holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
            capacity=ch_kwargs["packet"].get("batch_capacity", 4),
            policy=0,  # pop
        )

        packet_kwargs = ch_kwargs["packet"]
        net_connector_rx = rf_array.NetConnectorBasic(
            self,
            priority_stream_pool,
            name=f"{ch}_net_connector_rx",
            **packet_kwargs,
        )
        net_connector_rx.spec.inputs["burst_in"].connector(
            holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
            capacity=packet_kwargs.get("batch_capacity", 4),
            policy=0,  # pop
        )
        net_connector_rx.spec.outputs["rf_out"].connector(
            holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
            capacity=packet_kwargs.get("buffer_size", 4),
            policy=0,  # pop
        )
        self.add_flow(basic_net_rx, net_connector_rx, {("burst_out", "burst_in")})

        last_chunk_shape = (
            ch_kwargs["packet"]["num_samples"],
            ch_kwargs["packet"]["num_subchannels"],
        )
        last_buffer_capacity = packet_kwargs.get("buffer_size", 4)
        last_op = net_connector_rx

        if ch_kwargs["pipeline"]["selector"]:
            selector = rf_array.SubchannelSelect_sc16(
                self, cuda_stream_pool, name=f"{ch}_selector", **ch_kwargs["selector"]
            )
            selector.spec.inputs["rf_in"].connector(
                holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
                capacity=last_buffer_capacity,
                policy=0,  # pop
            )
            self.add_flow(last_op, selector)
            last_op = selector
            last_buffer_capacity = 1
            last_chunk_shape = (
                last_chunk_shape[0],
                len(ch_kwargs["selector"]["subchannel_idx"]),
            )

        if ch_kwargs["pipeline"]["converter"]:
            op_dict = {}

            converter = rf_array.TypeConversionComplexIntToFloat(
                self,
                cuda_stream_pool,
                name=f"{ch}_converter",
            )
            converter.spec.inputs["rf_in"].connector(
                holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
                capacity=last_buffer_capacity,
                policy=0,  # pop
            )
            self.add_flow(last_op, converter)
            last_op = converter
            last_buffer_capacity = 1
            op_dict["converter"] = (converter, last_chunk_shape)

            if ch_kwargs["pipeline"]["rotator"]:
                rotator = rf_array.RotatorScheduled(
                    self, cuda_stream_pool, name=f"{ch}_rotator", **ch_kwargs["rotator"]
                )
                self.add_flow(last_op, rotator)
                last_op = rotator
                op_dict["rotator"] = (rotator, last_chunk_shape)

            if ch_kwargs["pipeline"]["resampler0"]:
                resample_kwargs = add_chunk_kwargs(
                    last_chunk_shape, **ch_kwargs["resampler0"]
                )
                resampler0 = rf_array.ResamplePoly(
                    self, cuda_stream_pool, name=f"{ch}_resampler0", **resample_kwargs
                )
                self.add_flow(last_op, resampler0)
                last_op = resampler0
                last_chunk_shape = (
                    last_chunk_shape[0]
                    * resample_kwargs["up"]
                    // resample_kwargs["down"],
                    last_chunk_shape[1],
                )
                op_dict["resampler0"] = (resampler0, last_chunk_shape)

            if ch_kwargs["pipeline"]["resampler1"]:
                resample_kwargs = add_chunk_kwargs(
                    last_chunk_shape, **ch_kwargs["resampler1"]
                )
                resampler1 = rf_array.ResamplePoly(
                    self, cuda_stream_pool, name=f"{ch}_resampler1", **resample_kwargs
                )
                self.add_flow(last_op, resampler1)
                last_op = resampler1
                last_chunk_shape = (
                    last_chunk_shape[0]
                    * resample_kwargs["up"]
                    // resample_kwargs["down"],
                    last_chunk_shape[1],
                )
                op_dict["resampler1"] = (resampler1, last_chunk_shape)

            if ch_kwargs["pipeline"]["resampler2"]:
                resample_kwargs = add_chunk_kwargs(
                    last_chunk_shape, **ch_kwargs["resampler2"]
                )
                resampler2 = rf_array.ResamplePoly(
                    self, cuda_stream_pool, name=f"{ch}_resampler2", **resample_kwargs
                )
                self.add_flow(last_op, resampler2)
                last_op = resampler2
                last_chunk_shape = (
                    last_chunk_shape[0]
                    * resample_kwargs["up"]
                    // resample_kwargs["down"],
                    last_chunk_shape[1],
                )
                op_dict["resampler2"] = (resampler2, last_chunk_shape)

            self.add_spectrogram_flow(ch, cuda_stream_pool, op_dict)

            if ch_kwargs["pipeline"]["int_converter"]:
                int_converter = rf_array.TypeConversionComplexFloatToInt(
                    self,
                    cuda_stream_pool,
                    name=f"{ch}_int_converter",
                )
                self.add_flow(last_op, int_converter)
                last_op = int_converter

        if ch_kwargs["pipeline"]["digital_rf"]:
            if (
                ch_kwargs["pipeline"]["converter"]
                and not ch_kwargs["pipeline"]["int_converter"]
            ):
                drf_sink = rf_array.DigitalRFSink_fc32(
                    self,
                    cuda_stream_pool,
                    name=f"{ch}_drf_sink",
                    **add_chunk_kwargs(last_chunk_shape, **ch_kwargs["drf_sink"]),
                )
                drf_sink.spec.inputs["rf_in"].connector(
                    holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
                    capacity=25,
                    policy=0,  # pop
                )
                self.add_flow(last_op, drf_sink)
            else:
                drf_sink = rf_array.DigitalRFSink_sc16(
                    self,
                    cuda_stream_pool,
                    name=f"{ch}_drf_sink",
                    **add_chunk_kwargs(last_chunk_shape, **ch_kwargs["drf_sink"]),
                )
                drf_sink.spec.inputs["rf_in"].connector(
                    holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
                    capacity=25,
                    policy=0,  # pop
                )
                self.add_flow(last_op, drf_sink)

            if ch_kwargs["pipeline"]["metadata"]:
                dmd_sink = DigitalMetadataSink(
                    self,
                    name=f"{ch}_dmd_sink",
                    output_path=ch_kwargs["drf_sink"]["output_path"],
                    metadata_dir=f"{ch_kwargs['drf_sink']['channel_dir']}/metadata",
                    subdir_cadence_secs=ch_kwargs["drf_sink"]["subdir_cadence_secs"],
                    file_cadence_secs=ch_kwargs["drf_sink"]["file_cadence_millisecs"]
                    // 1000,
                    uuid=ch_kwargs["drf_sink"]["uuid"],
                    filename_prefix="metadata",
                    metadata=ch_kwargs["metadata"],
                )
                dmd_sink.spec.inputs["rf_in"].connector(
                    holoscan.core.IOSpec.ConnectorType.DOUBLE_BUFFER,
                    capacity=25,
                    policy=0,  # pop
                )
                self.add_flow(last_op, dmd_sink)

    def compose(self):
        cuda_stream_pool = holoscan.resources.CudaStreamPool(
            self,
            name="stream_pool",
            stream_flags=1,  # cudaStreamNonBlocking
            stream_priority=0,
            reserved_size=1,
            max_size=0,
        )
        priority_stream_pool = holoscan.resources.CudaStreamPool(
            self,
            name="priority_stream_pool",
            stream_flags=1,  # cudaStreamNonBlocking
            stream_priority=-2,  # lower means higher priority
            reserved_size=1,
            max_size=0,
        )

        for ch_idx in range(2):
            self.add_channel_flow(
                f"channel{ch_idx}", cuda_stream_pool, priority_stream_pool
            )


def main():
    parser = build_config_parser()
    cfg = parser.parse_args()

    logger = logging.getLogger("holoscan.simone_recorder")

    # We have a parsed configuration (using jsonargparse), but the holoscan app wants
    # to read all of its configuration parameters from a YAML file, so we write out
    # the configuration to a file in the temporary directory and feed it that
    tmp_config_dir = tempfile.TemporaryDirectory(prefix=os.path.basename(__file__))
    config_path = pathlib.Path(tmp_config_dir.name) / "recorder_config.yaml"
    logger.debug(f"Writing temporary config file to {config_path}")
    with config_path.open("w") as f:
        f.write(
            parser.dump(
                cfg,
                format="yaml",
                skip_none=True,
                skip_default=False,
                skip_link_targets=False,
            )
        )

    app = App([sys.executable, sys.argv[0]])
    app.config(str(config_path))

    scheduler = holoscan.schedulers.EventBasedScheduler(
        app,
        name="event-based-scheduler",
        **app.kwargs("scheduler"),
    )
    app.scheduler(scheduler)

    def sigterm_handler(signal, frame):
        logger.info("Received SIGTERM, cleaning up")
        sys.stdout.flush()
        tmp_config_dir.cleanup()
        sys.exit(128 + signal)

    signal.signal(signal.SIGTERM, sigterm_handler)

    try:
        app.run()
    except KeyboardInterrupt:
        # catch keyboard interrupt and simply exit
        tmp_config_dir.cleanup()
        logger.info("Done")
        sys.stdout.flush()
        # Holoscan graph execution framework handles all cleanup
        # so we just need to exit immediately without further Python cleanup
        # (which would result in a segfault from double free)
        os._exit(0)
    except SystemExit as e:
        tmp_config_dir.cleanup()
        # Holoscan graph execution framework handles all cleanup
        # so we just need to exit immediately without further Python cleanup
        # (which would result in a segfault from double free)
        os._exit(e.code)
    else:
        tmp_config_dir.cleanup()


if __name__ == "__main__":
    main()
