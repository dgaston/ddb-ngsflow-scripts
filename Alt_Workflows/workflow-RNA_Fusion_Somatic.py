#!/usr/bin/env python

# Standard packages
import sys
import argparse

# Third-party packages
from toil.job import Job

# Package methods
from ddb import configuration
from ddb_ngsflow import pipeline
from ddb_ngsflow.rna import star
from ddb_ngsflow.variation.sv import manta


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--samples_file', help="Input configuration file for samples")
    parser.add_argument('-c', '--configuration', help="Configuration file for various settings")
    Job.Runner.addToilOptions(parser)
    args = parser.parse_args()
    args.logLevel = "INFO"

    sys.stdout.write("Parsing configuration data\n")
    config = configuration.configure_runtime(args.configuration)

    sys.stdout.write("Parsing sample data\n")
    samples = configuration.configure_samples(args.samples_file, config)

    # Workflow Graph definition. The following workflow definition should create a valid Directed Acyclic Graph (DAG)
    root_job = Job.wrapJobFn(pipeline.spawn_batch_jobs, cores=1)

    # Per sample jobs
    for sample in samples:
        # Alignment and Refinement Stages
        # 1st Pass STAR

        flags = list()
        flags.append("compressed")

        align_job = Job.wrapJobFn(star.star_paired, config, sample, samples, flags,
                                  cores=int(config['star']['num_cores']),
                                  memory="{}G".format(config['star']['max_mem']))

        manta_job = Job.wrapJobFn(manta.manta, config, sample, samples,
                                  cores=int(config['manta']['num_cores']),
                                  memory="{}G".format(config['manta']['max_mem']))

        # Create workflow from created jobs
        root_job.addChild(align_job)
        align_job.addChild(manta_job)

    # Start workflow execution
    Job.Runner.startToil(root_job, args)
