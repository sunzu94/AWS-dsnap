import logging
import os
from queue import Queue, Empty
from threading import Thread
from typing import TYPE_CHECKING, List, Iterator, NamedTuple, IO, Optional

import botocore.config

if TYPE_CHECKING:
    from mypy_boto3_ebs.client import EBSClient
    from mypy_boto3_ebs.type_defs import BlockTypeDef
    from mypy_boto3_ec2.client import EC2Client
    from mypy_boto3_ec2.type_defs import SnapshotTypeDef

import boto3.session

MEGABYTE: int = 1024 * 1024
GIBIBYTE: int = 1024 * MEGABYTE

FETCH_THREADS = 30


class Block(NamedTuple):
    BlockData: IO[bytes]
    Offset: int


class Snapshot:
    def __init__(
            self,
            snapshot_id: str,
            boto3_session: boto3.session.Session = boto3.session.Session(region_name='us-east-1'),
            botocore_conf: Optional[botocore.config.Config] = botocore.config.Config()
    ) -> None:
        self.snapshot_id = snapshot_id
        self.output_file = ''

        self.queue: Queue = Queue()

        # Make sure the number of connections matches the number of threads we run when fetching the EBS snapshot
        ebs_config = botocore_conf.merge(botocore.config.Config(max_pool_connections=FETCH_THREADS))
        self.ebs: EBSClient = boto3_session.client('ebs', config=ebs_config)

        self.volume_size_b = 0
        self.total_blocks = 0
        self.blocks_written = 0
        self.block_size_b = 0

        self.get_blocks()

    def get_blocks(self) -> List['BlockTypeDef']:
        resp = self.ebs.list_snapshot_blocks(SnapshotId=self.snapshot_id)

        self.block_size_b = resp['BlockSize']
        self.volume_size_b = resp['VolumeSize'] * GIBIBYTE
        logging.info(f"Volume size is {self.volume_size_b}")

        blocks = resp['Blocks']

        while resp.get('NextToken'):
            resp = self.ebs.list_snapshot_blocks(SnapshotId=self.snapshot_id, NextToken=resp.get('NextToken'))
            blocks.extend(resp['Blocks'])

        self.total_blocks = len(blocks)
        logging.info(f"Number of blocks in image: {self.total_blocks}")

        return blocks

    def download(self, output_file: str):
        assert output_file
        self.output_file = os.path.abspath(output_file)

        with open(output_file, 'wb') as f:
            logging.info(f"Truncating file to {self.volume_size_b}")
            f.truncate(self.volume_size_b)
            f.flush()

        threads = list()
        for i in range(FETCH_THREADS):
            t = Thread(target=self._write_blocks_worker)
            threads.append(t)
            t.start()

        blocks = self.get_blocks()
        for block in blocks:
            logging.debug(f"Putting block index {block['BlockIndex']} on the queue")
            self.queue.put(block)

        self.queue.join()

        for t in threads:
            t.join()

        print(f"Output Path: {output_file}")

    def _write_blocks_worker(self):
        while self.total_blocks != self.blocks_written:
            try:
                block: 'BlockTypeDef' = self.queue.get(timeout=0.2)
                self.write_block(self.fetch_block(block))
                self.blocks_written += 1
                print(f"Saved block {self.blocks_written} of {self.total_blocks}", end='\r')
                self.queue.task_done()
            except Exception as e:
                if isinstance(e, Empty):
                    continue
                else:
                    logging.exception(f"[ERROR] {e.args}")
                    raise e

    def fetch_block(self, block: 'BlockTypeDef') -> Block:
        logging.debug(f"Getting block index {block['BlockIndex']}")
        resp = self.ebs.get_snapshot_block(
            SnapshotId=self.snapshot_id,
            BlockIndex=block['BlockIndex'],
            BlockToken=block['BlockToken'],
        )

        # BlockIndex is equal to 512 KiB and seek uses bytes.
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/ebs.html#EBS.Client.put_snapshot_block
        return Block(
            Offset=block['BlockIndex'] * self.block_size_b,
            BlockData=resp['BlockData'],
        )

    def write_block(self, block: Block) -> int:
        logging.debug(f"Writing block at offset {block.Offset}")
        """Takes a WriteBlock object to write to disk and yields the number of MiB's for each write."""
        with os.fdopen(os.open(self.output_file, os.O_RDWR | os.O_CREAT), 'rb+') as f:
            f.seek(block.Offset)
            bytes_written = f.write(block.BlockData.read())
            f.flush()
            return bytes_written