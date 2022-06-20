import sys
from collections import Counter
from argparse import ArgumentParser
import subprocess
import os
import concurrent.futures
from collections import defaultdict
import shared.param as param
from shared.vcf import VcfReader, VcfWriter
from shared.utils import str2bool

file_directory = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
main_entry = os.path.join(file_directory, "clair-somatic.py")

def get_base_list(columns):
    pileup_bases = columns[4]

    base_idx = 0
    base_list = []
    while base_idx < len(pileup_bases):
        base = pileup_bases[base_idx]
        if base == '+' or base == '-':
            base_idx += 1
            advance = 0
            while True:
                num = pileup_bases[base_idx]
                if num.isdigit():
                    advance = advance * 10 + int(num)
                    base_idx += 1
                else:
                    break
            base_list[-1][1] = base + pileup_bases[base_idx: base_idx + advance]  # add indel seq
            base_idx += advance - 1

        elif base in "ACGTNacgtn#*":
            base_list.append([base, ""])
        elif base == '^':  # start of read, next base is mq, update mq info
            base_idx += 1
        # skip $, the end of read
        base_idx += 1
    # base_counter = Counter([''.join(item) for item in base_list])
    upper_base_counter = Counter([''.join(item).upper() for item in base_list])
    return upper_base_counter, base_list

def extract_base(POS):

    pos = POS.pos
    ref_base = POS.reference_bases
    alt_base = POS.alternate_bases[0]

    bam_fn = args.bam_fn
    ref_fn = args.ref_fn
    samtools = args.samtools
    ctg_name = args.ctg_name
    min_mq = args.min_mq
    min_bq = args.min_bq
    python = args.python

    ctg_range = "{}:{}-{}".format(ctg_name, pos, pos)
    samtools_command = "{} mpileup {} --min-MQ {} --min-BQ {} --excl-flags 2316 -r {}".format(samtools, bam_fn, min_mq, min_bq, ctg_range)

    output = subprocess.Popen(samtools_command, shell=True, stdout=subprocess.PIPE, universal_newlines=True)
    output = output.stdout.read().rstrip()

    columns = output.split('\t')
    # print(output)
    if len(columns) < 4:
        return pos, True, (-1,-1,-1,-1)
    base_counter, base_list = get_base_list(columns)

    realign_command = "{} {} realign_reads --pos {} --ctg_name {} --bam_fn {} --ref_fn {}".format(python, main_entry, pos, ctg_name, bam_fn, ref_fn)
    samtools_pile_command = "{} mpileup - --reverse-del --min-MQ {} --min-BQ {} --excl-flags 2316 | grep -w {}".format(samtools, min_mq, min_bq, pos)
    realign_command += " | " + samtools_pile_command
    realign_output = subprocess.Popen(realign_command, shell=True, stdout=subprocess.PIPE, universal_newlines=True )

    realign_output = realign_output.stdout.read().rstrip()
    # print(realign_output)
    columns = realign_output.split('\t')
    if len(columns) < 4:
        return pos, True, (-1,-1,-1,-1)
    realign_base_counter, realign_base_list = get_base_list(columns)

    raw_depth = len(base_list)
    realign_depth = len(realign_base_list)
    raw_support_read_num = base_counter[alt_base]
    realign_support_read_num = realign_base_counter[alt_base]

    pass_realign_filter = True
    if raw_support_read_num / float(raw_depth) > realign_support_read_num / realign_depth and realign_support_read_num < raw_support_read_num:
        pass_realign_filter = False
    return pos, pass_realign_filter, (raw_support_read_num, raw_depth, realign_support_read_num, realign_depth)


def realign_variants(args):

    ctg_name = args.ctg_name
    input_vcf_fn = args.input_vcf_fn
    threads = args.threads

    input_vcf_reader = VcfReader(vcf_fn=input_vcf_fn, ctg_name=ctg_name, show_ref=False, keep_row_str=True,
                                 filter_tag="PASS;HighConf,PASS;MedConf,PASS,RefCall", save_header=True) #PASS;HighConf PASS;MedConf
    input_vcf_reader.read_vcf()
    input_variant_dict = input_vcf_reader.variant_dict

    output_vcf_fn = args.output_vcf_fn
    vcf_writer = VcfWriter(vcf_fn=output_vcf_fn, ref_fn=args.ref_fn, ctg_name=ctg_name, show_ref_calls=True)

    # for POS in input_variant_dict.values():
    #     pos, pass_realign_filter = extract_base(POS)

    realign_fail_pos_set = set()
    realign_info_dict = defaultdict()
    with concurrent.futures.ProcessPoolExecutor(max_workers=threads) as exec:
        for result in exec.map(extract_base, list(input_variant_dict.values())):
            pos, pass_realign_filter = result[:2]
            raw_support_read_num, raw_depth, realign_support_read_num, realign_depth = result[2]
            realign_info_dict[(ctg_name, pos)] = result[2]

            if pass_realign_filter is False:
                realign_fail_pos_set.add((ctg_name, pos))

    for pos in sorted(input_variant_dict.keys()):
        row_str = input_variant_dict[pos].row_str.rstrip()
        if (ctg_name, pos) in realign_fail_pos_set:
            row_str = row_str.replace("PASS", "Low_realign")
        if args.output_realign_info:
            row_str += "\t" + ' '.join([str(item) for item in realign_info_dict[(ctg_name, pos)]])
        vcf_writer.vcf_writer.write(row_str + '\n')
    vcf_writer.close()
    print("[INFO] {}: {} called variant filtered by realignment".format(ctg_name, len(realign_fail_pos_set)))


def main():
    parser = ArgumentParser(description="Reads realignment")

    parser.add_argument('--bam_fn', type=str, default=None,
                        help="Sorted BAM file input, required")

    parser.add_argument('--ref_fn', type=str, default="ref.fa",
                        help="Reference fasta file input, required")

    parser.add_argument('--read_fn', type=str, default="PIPE",
                        help="Output realigned BAM. Default directly pass reads to CreateTensor_phasing using PIPE. Default: %(default)s")

    parser.add_argument('--ctg_name', type=str, default=None,
                        help="The name of sequence to be processed")

    parser.add_argument('--input_vcf_fn', type=str, default=None,
                        help="The 1-based starting position of the sequence to be processed")

    parser.add_argument('--output_vcf_fn', type=str, default=None,
                        help="The 1-based starting position of the sequence to be processed")

    # options for advanced users
    parser.add_argument('--min_mq', type=int, default=param.min_mq,
                        help="EXPERIMENTAL: If set, reads with mapping quality with <$min_mq are filtered, default: %(default)d")

    parser.add_argument('--min_bq', type=int, default=param.min_bq,
                        help="EXPERIMENTAL: If set, bases with base quality with <$min_bq are filtered, default: %(default)d")

    # options for debug purpose
    parser.add_argument('--samtools', type=str, default="samtools",
                        help="Path to the 'samtools', samtools version >= 1.10 is required. default: %(default)s")

    parser.add_argument('--threads', type=int, default=1,
                        help="Path to the 'samtools', samtools version >= 1.10 is required. default: %(default)s")

    parser.add_argument('--pos', type=int, default=None,
                        help="Output VCF filename, required")

    parser.add_argument('--python', type=str, default="python3",
                        help="Path to the 'python3', default: %(default)s")

    parser.add_argument('--bed_fn', type=str, default=None,
                        help="Call variant only in the provided regions. Will take an intersection if --ctg_name and/or (--ctg_start, --ctg_end) are set")

    parser.add_argument('--input_filter_tag', type=str, default="PASS:HighConf",
                        help='Output VCF filename, required')

    ## Test in specific candidate position. Only for testing
    parser.add_argument('--output_realign_info', type=str2bool, default=0,
                        help="")

    # if len(sys.argv[1:]) == 0:
    #     parser.print_help()
    #     sys.exit(1)
    global args
    args = parser.parse_args()

    realign_variants(args)


if __name__ == "__main__":
    main()

