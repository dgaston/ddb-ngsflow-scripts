#!/usr/bin/env python

# Standard packages
import sys
import argparse

# Third-party packages
from toil.job import Job

# Package methods
from ddb import configuration
from ddb_ngsflow import gatk
from ddb_ngsflow import annotation
from ddb_ngsflow import pipeline
from ddb_ngsflow.align import bwa
from ddb_ngsflow.variation import variation
from ddb_ngsflow.variation import haplotypecaller


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
        align_job = Job.wrapJobFn(bwa.run_bwa_mem, config, sample, samples,
                                  cores=int(config['bwa']['num_cores']),
                                  memory="{}G".format(config['bwa']['max_mem']))

        add_job = Job.wrapJobFn(gatk.add_or_replace_readgroups, config, sample, align_job.rv(),
                                cores=1,
                                memory="{}G".format(config['gatk']['max_mem']))

        dedup_job = Job.wrapJobFn(gatk.mark_duplicates, config, sample, add_job.rv(),
                                  cores=int(config['gatk']['num_cores']),
                                  memory="{}G".format(config['gatk']['max_mem']))

        creator_job = Job.wrapJobFn(gatk.realign_target_creator, config, sample, dedup_job.rv(),
                                    cores=int(config['gatk']['num_cores']),
                                    memory="{}G".format(config['gatk']['max_mem']))

        realign_job = Job.wrapJobFn(gatk.realign_indels, config, sample, add_job.rv(), creator_job.rv(),
                                    cores=1,
                                    memory="{}G".format(config['gatk']['max_mem']))

        recal_job = Job.wrapJobFn(gatk.recalibrator, config, sample, realign_job.rv(),
                                  cores=int(config['gatk']['num_cores']),
                                  memory="{}G".format(config['gatk']['max_mem']))
        # Variant Calling
        haplotypecaller_job = Job.wrapJobFn(haplotypecaller.haplotypecaller_single, config, sample, samples,
                                            recal_job.rv(),
                                            cores=1,
                                            memory="{}G".format(config['freebayes']['max_mem']))

        # Create workflow from created jobs
        root_job.addChild(align_job)
        align_job.addChild(add_job)
        add_job.addChild(dedup_job)
        dedup_job.addChild(creator_job)
        creator_job.addChild(realign_job)
        realign_job.addChild(recal_job)
        recal_job.addChild(haplotypecaller_job)

    # Need to filter for on target only results somewhere as well
    joint_call_job = Job.wrapJobFn(haplotypecaller.joint_variant_calling, config, sample, samples)

    normalization_job = Job.wrapJobFn(variation.vt_normalization, config, sample, "freebayes",
                                      joint_call_job.rv(),
                                      cores=1,
                                      memory="{}G".format(config['gatk']['max_mem']))

    gatk_annotate_job = Job.wrapJobFn(gatk.annotate_vcf, config, sample, normalization_job.rv(), recal_job.rv(),
                                      cores=int(config['gatk']['num_cores']),
                                      memory="{}G".format(config['gatk']['max_mem']))

    gatk_filter_job = Job.wrapJobFn(gatk.filter_variants, config, sample, gatk_annotate_job.rv(),
                                    cores=1,
                                    memory="{}G".format(config['gatk']['max_mem']))

    snpeff_job = Job.wrapJobFn(annotation.snpeff, config, sample, gatk_filter_job.rv(),
                               cores=int(config['snpeff']['num_cores']),
                               memory="{}G".format(config['snpeff']['max_mem']))

    gemini_job = Job.wrapJobFn(annotation.gemini, config, sample, gatk_filter_job.rv(),
                               cores=int(config['snpeff']['num_cores']),
                               memory="{}G".format(config['snpeff']['max_mem']))

    root_job.addFollowOn(joint_call_job)
    joint_call_job.addChild(normalization_job)
    normalization_job.addChild(gatk_annotate_job)
    gatk_annotate_job.addChild(gatk_filter_job)
    gatk_filter_job.addChild(snpeff_job)
    snpeff_job.addChild(gemini_job)

    # Start workflow execution
    Job.Runner.startToil(root_job, args)