import csv
import utils

from datetime import datetime
from collections import defaultdict
from cassandra.cqlengine import connection
from cassandra import WriteFailure
from cassandra import InvalidRequest

from coveragestore import AmpliconCoverage
from coveragestore import SampleCoverage
from variantstore import SampleVariant
from variantstore import Variant


def process_sample_coverage(job, addresses, keyspace, auth, sample, program, samples):
    connection.setup(addresses, keyspace, auth_provider=auth)

    with open("{}.sambamba_coverage.bed".format(samples[sample]['library_name']), 'rb') as coverage:
        reader = csv.reader(coverage, delimiter='\t')
        header = reader.next()
        threshold_indices = list()
        thresholds = list()
        index = 0
        for element in header:
            if element.startswith("percentage"):
                threshold = element.replace('percentage', '')
                threshold_indices.append(index)
                thresholds.append(int(threshold))
            index += 1

        for row in reader:
            threshold_data = defaultdict(float)
            index = 0
            for threshold in thresholds:
                threshold_data[threshold] = row[threshold_indices[index]]
                index += 1

            sample_data = SampleCoverage.create(sample=samples[sample]['sample_name'],
                                                library_name=samples[sample]['library_name'],
                                                run_id=samples[sample]['run_id'],
                                                num_libraries_in_run=samples[sample]['num_libraries_in_run'],
                                                sequencer_id=samples[sample]['sequencer'],
                                                program_name=program,
                                                extraction=samples[sample]['extraction'],
                                                panel=samples[sample]['panel'],
                                                target_pool=samples[sample]['target_pool'],
                                                amplicon=row[3],
                                                num_reads=row[4],
                                                mean_coverage=row[5],
                                                thresholds=thresholds,
                                                perc_bp_cov_at_thresholds=threshold_data)

            amplicon_data = AmpliconCoverage.create(amplicon=row[3],
                                                    sample=samples[sample]['sample_name'],
                                                    library_name=samples[sample]['library_name'],
                                                    run_id=samples[sample]['run_id'],
                                                    num_libraries_in_run=samples[sample]['num_libraries_in_run'],
                                                    sequencer_id=samples[sample]['sequencer'],
                                                    program_name=program,
                                                    extraction=samples[sample]['extraction'],
                                                    panel=samples[sample]['panel'],
                                                    target_pool=samples[sample]['target_pool'],
                                                    num_reads=row[4],
                                                    mean_coverage=row[5],
                                                    thresholds=thresholds,
                                                    perc_bp_cov_at_thresholds=threshold_data)


def process_sample(job, addresses, keyspace, authenticator, parse_functions, sample, samples, config):
    connection.setup(addresses, keyspace, auth_provider=authenticator)

    caller_records = defaultdict(lambda: dict())

    sys.stdout.write("Parsing Caller VCF Files\n")
    vcf_parsing.parse_vcf("{}.mutect.normalized.vcf".format(sample), "mutect", caller_records)
    vcf_parsing.parse_vcf("{}.vardict.normalized.vcf".format(sample), "vardict", caller_records)
    vcf_parsing.parse_vcf("{}.freebayes.normalized.vcf".format(sample), "freebayes", caller_records)
    vcf_parsing.parse_vcf("{}.scalpel.normalized.vcf".format(sample), "scalpel", caller_records)
    vcf_parsing.parse_vcf("{}.platypus.normalized.vcf".format(sample), "platypus", caller_records)
    vcf_parsing.parse_vcf("{}.pindel.normalized.vcf".format(sample), "pindel", caller_records)

    annotated_vcf = "{}.vcfanno.snpEff.GRCh37.75.vcf".format(sample)

    sys.stdout.write("Parsing VCFAnno VCF\n")
    vcf = VCF(annotated_vcf)

    sys.stdout.write("Parsing VCFAnno VCF with CyVCF2\n")
    reader = cyvcf2.VCFReader(annotated_vcf)
    desc = reader["ANN"]["Description"]
    annotation_keys = [x.strip("\"'") for x in re.split("\s*\|\s*", desc.split(":", 1)[1].strip('" '))]

    # Filter out variants with minor allele frequencies above the threshold but
    # retain any that are above the threshold but in COSMIC or in ClinVar and not listed as benign.
    sys.stdout.write("Processing individual variants\n")
    added = 0
    failed = 0
    for variant in vcf:
        # Parsing VCF and creating data structures for Cassandra model
        callers = variant.INFO.get('CALLERS').split(',')
        effects = utils.get_effects(variant, annotation_keys)
        top_impact = utils.get_top_impact(effects)
        population_freqs = utils.get_population_freqs(variant)
        amplicon_data = utils.get_amplicon_data(variant)

        key = (unicode("chr{}".format(variant.CHROM)), int(variant.start), int(variant.end), unicode(variant.REF),
               unicode(variant.ALT[0]))

        caller_variant_data_dicts = defaultdict(dict)
        max_som_aaf = -1.00
        max_depth = -1
        min_depth = 100000000

        for caller in callers:
            caller_variant_data_dicts[caller] = parse_functions[caller](caller_records[caller][key])
            if float(caller_variant_data_dicts[caller]['AAF']) > max_som_aaf:
                max_som_aaf = float(caller_variant_data_dicts[caller]['AAF'])
            if int(caller_variant_data_dicts[caller]['DP']) < min_depth:
                min_depth = int(caller_variant_data_dicts[caller]['DP'])
            if int(caller_variant_data_dicts[caller]['DP']) > max_depth:
                max_depth = int(caller_variant_data_dicts[caller]['DP'])

        if min_depth == 100000000:
            min_depth = -1

        # Create Cassandra Objects
        # Create the general variant ordered table
        try:
            cassandra_variant = Variant.create(
                    reference_genome=config['genome_version'],
                    chr=variant.CHROM,
                    pos=variant.start,
                    end=variant.end,
                    ref=variant.REF,
                    alt=variant.ALT[0],
                    sample=samples[sample]['sample_name'],
                    extraction=samples[sample]['extraction'],
                    library_name=samples[sample]['library_name'],
                    run_id=samples[sample]['run_id'],
                    panel_name=samples[sample]['panel'],
                    # initial_report_panel=samples[sample]['report'],
                    target_pool=samples[sample]['target_pool'],
                    sequencer=samples[sample]['sequencer'],
                    rs_id=variant.ID,
                    date_annotated=datetime.now(),
                    subtype=variant.INFO.get('sub_type'),
                    type=variant.INFO.get('type'),

                    gene=top_impact.gene,
                    transcript=top_impact.transcript,
                    exon=top_impact.exon,
                    codon_change=top_impact.codon_change,
                    biotype=top_impact.biotype,
                    aa_change=top_impact.aa_change,
                    severity=top_impact.effect_severity,
                    impact=top_impact.top_consequence,
                    impact_so=top_impact.so,

                    max_maf_all=variant.INFO.get('max_aaf_all') or -1,
                    max_maf_no_fin=variant.INFO.get('max_aaf_no_fin') or -1,
                    transcripts_data=utils.get_transcript_effects(effects),
                    clinvar_data=utils.get_clinvar_info(variant),
                    cosmic_data=utils.get_cosmic_info(variant),
                    in_clinvar=vcf_parsing.var_is_in_clinvar(variant),
                    in_cosmic=vcf_parsing.var_is_in_cosmic(variant),
                    is_pathogenic=vcf_parsing.var_is_pathogenic(variant),
                    is_lof=vcf_parsing.var_is_lof(variant),
                    is_coding=vcf_parsing.var_is_coding(variant),
                    is_splicing=vcf_parsing.var_is_splicing(variant),
                    rs_ids=vcf_parsing.parse_rs_ids(variant),
                    cosmic_ids=vcf_parsing.parse_cosmic_ids(variant),
                    callers=callers,
                    population_freqs=population_freqs,
                    amplicon_data=amplicon_data,
                    max_som_aaf=max_som_aaf,
                    min_depth=min_depth,
                    max_depth=max_depth,
                    mutect=caller_variant_data_dicts['mutect'] or dict(),
                    freebayes=caller_variant_data_dicts['freebayes'] or dict(),
                    scalpel=caller_variant_data_dicts['scalpel'] or dict(),
                    platypus=caller_variant_data_dicts['platypus'] or dict(),
                    pindel=caller_variant_data_dicts['pindel'] or dict(),
                    vardict=caller_variant_data_dicts['vardict'] or dict(),
                    manta=caller_variant_data_dicts['manta'] or dict()
                    )
        except WriteFailure:
            with open("{}.sample_variant_add.log".format(samples[sample]['library_name']), "a") as err:
                err.write("Failed to write variant to variantstore:\n")
                err.write("Sample: {}\t Library: {}\n".format(samples[sample]['sample_name'],
                                                              samples[sample]['library_name'],))
                err.write("{}\n".format(variant))
        except InvalidRequest:
            with open("{}.sample_variant_add.log".format(samples[sample]['library_name']), "a") as err:
                err.write("Failed to write variant to variantstore:\n")
                err.write("Sample: {}\t Library: {}\n".format(samples[sample]['sample_name'],
                                                              samples[sample]['library_name'], ))
                err.write("{}\n".format(variant))

        # Create Cassandra Object
        try:
            sample_variant = SampleVariant.create(
                    sample=samples[sample]['sample_name'],
                    run_id=samples[sample]['run_id'],
                    library_name=samples[sample]['library_name'],
                    reference_genome=config['genome_version'],
                    chr=variant.CHROM,
                    pos=variant.start,
                    end=variant.end,
                    ref=variant.REF,
                    alt=variant.ALT[0],
                    extraction=samples[sample]['extraction'],
                    panel_name=samples[sample]['panel'],
                    # initial_report_panel=samples[sample]['report'],
                    target_pool=samples[sample]['target_pool'],
                    sequencer=samples[sample]['sequencer'],
                    rs_id=variant.ID,
                    date_annotated=datetime.now(),
                    subtype=variant.INFO.get('sub_type'),
                    type=variant.INFO.get('type'),
                    gene=top_impact.gene,
                    transcript=top_impact.transcript,
                    exon=top_impact.exon,
                    codon_change=top_impact.codon_change,
                    biotype=top_impact.biotype,
                    aa_change=top_impact.aa_change,
                    severity=top_impact.effect_severity,
                    impact=top_impact.top_consequence,
                    impact_so=top_impact.so,
                    max_maf_all=variant.INFO.get('max_aaf_all') or -1,
                    max_maf_no_fin=variant.INFO.get('max_aaf_no_fin') or -1,
                    transcripts_data=utils.get_transcript_effects(effects),
                    clinvar_data=utils.get_clinvar_info(variant),
                    cosmic_data=utils.get_cosmic_info(variant),
                    in_clinvar=vcf_parsing.var_is_in_clinvar(variant),
                    in_cosmic=vcf_parsing.var_is_in_cosmic(variant),
                    is_pathogenic=vcf_parsing.var_is_pathogenic(variant),
                    is_lof=vcf_parsing.var_is_lof(variant),
                    is_coding=vcf_parsing.var_is_coding(variant),
                    is_splicing=vcf_parsing.var_is_splicing(variant),
                    rs_ids=vcf_parsing.parse_rs_ids(variant),
                    cosmic_ids=vcf_parsing.parse_cosmic_ids(variant),
                    callers=callers,
                    population_freqs=population_freqs,
                    amplicon_data=amplicon_data,
                    max_som_aaf=max_som_aaf,
                    min_depth=min_depth,
                    max_depth=max_depth,
                    mutect=caller_variant_data_dicts['mutect'] or dict(),
                    freebayes=caller_variant_data_dicts['freebayes'] or dict(),
                    scalpel=caller_variant_data_dicts['scalpel'] or dict(),
                    platypus=caller_variant_data_dicts['platypus'] or dict(),
                    pindel=caller_variant_data_dicts['pindel'] or dict(),
                    vardict=caller_variant_data_dicts['vardict'] or dict(),
                    manta=caller_variant_data_dicts['manta'] or dict()
                    )
        except WriteFailure:
            failed += 1
            with open("{}.sample_variant_add.log".format(samples[sample]['library_name']), "a") as err:
                err.write("Failed to write variant to variantstore:\n")
                err.write("Sample: {}\t Library: {}\n".format(samples[sample]['sample_name'],
                                                              samples[sample]['library_name'],))
                err.write("{}\n".format(variant))
        except InvalidRequest:
            with open("{}.sample_variant_add.log".format(samples[sample]['library_name']), "a") as err:
                err.write("Failed to write variant to variantstore:\n")
                err.write("Sample: {}\t Library: {}\n".format(samples[sample]['sample_name'],
                                                              samples[sample]['library_name'], ))
        added += 1

    with open("{}.sample_variant_add.log".format(samples[sample]['library_name']), "a") as err:
        err.write("Sample: {}\t Library: {}\n".format(samples[sample]['sample_name'],
                                                      samples[sample]['library_name'], ))
        err.write("Wrote {} variants to variantstore\n".format(added))
        err.write("Failed to add {} variants to variantstore\n".format(failed))

    job.fileStore.logToMaster("Variant data for {} variants saved to Cassandra for sample {}."
                              "{} variants failed to add to database\n".format(added, sample, failed))
