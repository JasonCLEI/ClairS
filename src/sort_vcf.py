import os
import subprocess
import shlex
from sys import stdin, exit
from argparse import ArgumentParser
from collections import defaultdict

from shared.utils import log_error, log_warning, file_path_from, subprocess_popen, str2bool
from shared.vcf import vcf_header

major_contigs_order = ["chr" + str(a) for a in list(range(1, 23)) + ["X", "Y"]] + [str(a) for a in
                                                                                   list(range(1, 23)) + ["X", "Y"]]


def compress_index_vcf(input_vcf):
    # use bgzip to compress vcf -> vcf.gz
    # use tabix to index vcf.gz
    proc = subprocess.run('bgzip -f {}'.format(input_vcf), shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run('tabix -f -p vcf {}.gz'.format(input_vcf), shell=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def output_header(output_fn, reference_file_path, sample_name='SAMPLE'):
    output_file = open(output_fn, "w")
    output_file.write(vcf_header)

    if reference_file_path is not None:
        reference_index_file_path = file_path_from(reference_file_path, suffix=".fai", exit_on_not_found=True, sep='.')
        with open(reference_index_file_path, "r") as fai_fp:
            for row in fai_fp:
                columns = row.strip().split("\t")
                contig_name, contig_size = columns[0], columns[1]
                output_file.write(("##contig=<ID=%s,length=%s>" % (contig_name, contig_size) + '\n'))

    output_file.write('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t%s' % (sample_name))
    output_file.close()


def print_calling_step(output_fn=""):
    merge_output = os.path.join(os.path.dirname(output_fn), 'merge_output.vcf.gz')
    pileup_output = os.path.join(os.path.dirname(output_fn), 'pileup.vcf.gz')


def sort_vcf_from_stdin(args):
    """
    Sort vcf file according to variants start position and contig name.
    """

    row_count = 0
    header = []
    contig_dict = defaultdict(defaultdict)
    no_vcf_output = True
    for row in stdin:
        row_count += 1
        if row[0] == '#':
            if row not in header:
                header.append(row)
            continue
        # use the first vcf header
        columns = row.strip().split(maxsplit=3)
        ctg_name, pos = columns[0], columns[1]
        contig_dict[ctg_name][int(pos)] = row
        no_vcf_output = False
    if row_count == 0:
        print(log_warning("[WARNING] No vcf file found, please check the setting"))
    if no_vcf_output:
        print(log_warning("[WARNING] No variant found, please check the setting"))

    contigs_order = major_contigs_order + list(contig_dict.keys())
    contigs_order_list = sorted(contig_dict.keys(), key=lambda x: contigs_order.index(x))
    with open(args.output_fn, 'w') as output:
        output.write(''.join(header))
        for contig in contigs_order_list:
            all_pos = sorted(contig_dict[contig].keys())
            for pos in all_pos:
                output.write(contig_dict[contig][pos])


def sort_bed_from_stdin(args):
    """
    Sort vcf file according to variants start position and contig name.
    """

    row_count = 0
    header = []
    contig_dict = defaultdict(defaultdict)
    no_vcf_output = True
    for row in stdin:
        row_count += 1
        if row[0] == '#':
            if row not in header:
                header.append(row)
            continue
        # use the first vcf header
        columns = row.strip().split()
        ctg_name, bed_start, bed_end = columns[:3]
        contig_dict[ctg_name][(int(bed_start), int(bed_end))] = row
        no_bed_output = False
    if row_count == 0:
        print(log_warning("[WARNING] No BED file found, please check the setting"))
    if no_bed_output:
        print(log_warning("[WARNING] No BED found, please check the setting"))

    contigs_order = major_contigs_order + list(contig_dict.keys())
    contigs_order_list = sorted(contig_dict.keys(), key=lambda x: contigs_order.index(x))
    with open(args.output_fn, 'w') as output:
        output.write(''.join(header))
        for contig in contigs_order_list:
            all_pos = sorted(contig_dict[contig].keys())
            for pos in all_pos:
                output.write(contig_dict[contig][pos])


def sort_vcf_from(args):
    """
    Sort vcf file from providing vcf filename prefix.
    """
    output_fn = args.output_fn
    input_dir = args.input_dir
    vcf_fn_prefix = args.vcf_fn_prefix
    vcf_fn_suffix = args.vcf_fn_suffix
    sample_name = args.sampleName
    ref_fn = args.ref_fn
    contigs_fn = args.contigs_fn
    compress_vcf = args.compress_vcf

    print("[INFO] Sorting VCFs...")

    if not os.path.exists(input_dir):
        exit(log_error("[ERROR] Input directory: {} not exists!").format(input_dir))
    all_files = os.listdir(input_dir)

    if vcf_fn_prefix is not None:
        all_files = [item for item in all_files if item.startswith(vcf_fn_prefix)]
        if len(all_files) == 0:
            output_header(output_fn=output_fn, reference_file_path=ref_fn, sample_name=sample_name)
            print(log_warning(
                "[WARNING] No vcf file found with prefix:{}/{}, output empty vcf file".format(input_dir,
                                                                                              vcf_fn_prefix)))
            if compress_vcf:
                compress_index_vcf(output_fn)
            print_calling_step(output_fn=output_fn)
            return

    if vcf_fn_suffix is not None:
        all_files = [item for item in all_files if item.endswith(vcf_fn_suffix)]
        if len(all_files) == 0:
            output_header(output_fn=output_fn, reference_file_path=ref_fn, sample_name=sample_name)
            print(log_warning(
                "[WARNING] No vcf file found with suffix:{}/{}, output empty vcf file".format(input_dir,
                                                                                              vcf_fn_prefix)))
            compress_index_vcf(output_fn)
            print_calling_step(output_fn=output_fn)
            return

    all_contigs_list = []
    if contigs_fn and os.path.exists(contigs_fn):
        with open(contigs_fn) as f:
            all_contigs_list = [item.rstrip() for item in f.readlines()]
    else:
        exit(log_error("[ERROR] Cannot find contig file {}. Exit!").format(contigs_fn))

    contigs_order = major_contigs_order + all_contigs_list
    contigs_order_list = sorted(all_contigs_list, key=lambda x: contigs_order.index(x))

    row_count = 0
    header = []
    no_vcf_output = True
    need_write_header = True

    output = open(output_fn, 'w')
    for contig in contigs_order_list:
        contig_dict = defaultdict(str)
        contig_vcf_fns = [fn for fn in all_files if contig in fn]
        for vcf_fn in contig_vcf_fns:
            file = os.path.join(input_dir, vcf_fn)

            fn = open(file, 'r')
            for row in fn:
                row_count += 1
                if row[0] == '#':
                    if row not in header:
                        header.append(row)
                    continue
                # use the first vcf header
                columns = row.strip().split(maxsplit=3)
                ctg_name, pos = columns[0], columns[1]
                # skip vcf file sharing same contig prefix, ie, chr1 and chr11
                if ctg_name != contig:
                    break
                contig_dict[int(pos)] = row
                no_vcf_output = False
            fn.close()

        if need_write_header and len(header):
            output.write(''.join(header))
            need_write_header = False
        all_pos = sorted(contig_dict.keys())
        for pos in all_pos:
            output.write(contig_dict[pos])

    output.close()

    if row_count == 0:
        print(log_warning("[WARNING] No vcf file found, output empty vcf file"))
        output_header(output_fn=output_fn, reference_file_path=ref_fn, sample_name=sample_name)
        if compress_vcf:
            compress_index_vcf(output_fn)
        print_calling_step(output_fn=output_fn)
        return
    if no_vcf_output:
        output_header(output_fn=output_fn, reference_file_path=ref_fn, sample_name=sample_name)
        print(log_warning("[WARNING] No variant found, output empty vcf file"))
        if compress_vcf:
            compress_index_vcf(output_fn)
        return

    if compress_vcf:
        compress_index_vcf(output_fn)

    print("[INFO] Finished VCF sorting!")


def main():
    parser = ArgumentParser(description="Sort a VCF file according to contig name and starting position")

    parser.add_argument('--output_fn', type=str, default=None, required=True,
                        help="Output VCF filename, required")

    parser.add_argument('--input_dir', type=str, default=None,
                        help="Input directory")

    parser.add_argument('--vcf_fn_prefix', type=str, default=None,
                        help="Input vcf filename prefix")

    parser.add_argument('--vcf_fn_suffix', type=str, default='.vcf',
                        help="Input vcf filename suffix")

    parser.add_argument('--ref_fn', type=str, default=None,
                        help="Reference fasta file input")

    parser.add_argument('--sampleName', type=str, default="SAMPLE",
                        help="Define the sample name to be shown in the VCF file, optional")

    parser.add_argument('--contigs_fn', type=str, default=None,
                        help="Contigs file with all processing contigs")

    parser.add_argument('--compress_vcf', type=str2bool, default=False,
                        help="Only work for gvcf file, reduce hard disk space")

    parser.add_argument('--bed_format', action='store_true',
                        help="Only work for gvcf file, reduce hard disk space")

    args = parser.parse_args()
    if args.input_dir is None:
        if args.bed_format:
            sort_bed_from_stdin(args)
        else:
            sort_vcf_from_stdin(args)
    else:
        # default entry
        sort_vcf_from(args)


if __name__ == "__main__":
    main()
