import sys
import shlex
import os
import json
import logging
import random
from subprocess import PIPE
from os.path import isfile
from copy import deepcopy
from argparse import ArgumentParser, SUPPRESS
from collections import Counter, defaultdict, OrderedDict

import shared.param as param
from shared.utils import subprocess_popen, file_path_from, IUPAC_base_to_num_dict as BASE2NUM, region_from, \
    reference_sequence_from, str2bool, vcf_candidates_from
from shared.interval_tree import bed_tree_from, is_region_in
from shared.intervaltree.intervaltree import IntervalTree

logging.basicConfig(format='%(message)s', level=logging.INFO)
BASES = set(list(BASE2NUM.keys()) + ["-"])
no_of_positions = param.no_of_positions
flanking_base_num = param.flankingBaseNum


BASE2NUMBER = dict(zip("ACGTURYSWKMBDHVN-", (0, 1, 2, 3, 3, 0, 1, 1, 0, 2, 0, 1, 0, 0, 0, 0, 4)))
NORMALIZE_NUM = param.NORMALIZE_NUM
HAP_TYPE = dict(zip((1, 0, 2), (30, 60, 90)))  # hap1 UNKNOWN H2
ACGT_NUM = dict(zip("ACGT+-*#N", (100, 25, 75, 50, -50, -100, 0, 0, 100)))

#           0    1    2    3    4    5    6    7     8    9    10   11  12   13    14  15   16    17  18      19      20      21
channel = ['A', 'C', 'G', 'T', 'I', 'I1', 'D', 'D1', '*', 'a', 'c', 'g','t', 'i', 'i1','d', 'd1','#']
# channel = ['A', 'C', 'G', 'T', 'I', 'D', '*', 'a', 'c', 'g','t', 'i','d','#']
channel += [
 'ALMQ', 'CLMQ', 'GLMQ', 'TLMQ', 'aLMQ', 'cLMQ', 'gLMQ', 'tLMQ', 'ALBQ', 'CLBQ', 'GLBQ', 'TLBQ', 'aLBQ', 'cLBQ', 'gLBQ', 'tLBQ']

phase_channel = [
 'AHP1', 'CHP1', 'GHP1', 'THP1', 'aHP1', 'cHP1', 'gHP1', 'tHP1', 'AHP2', 'CHP2', 'GHP2', 'THP2', 'aHP2', 'cHP2', 'gHP2', 'tHP2']
channel_size = len(channel)
BASE2INDEX = dict(zip(channel, tuple(range(channel_size))))
base_index = dict(zip(phase_channel, list(range(16))))

def phredscore2raw_score(qual):
    return ord(qual) - 33


def evc_base_from(base):
    if base == 'N':
        return 'A'
    elif base == 'n':
        return 'a'
    elif base in 'ACGTacgt':
        return base
    elif base.isupper():
        return 'A'
    else:
        return 'a'


def sorted_by_hap_read_name(center_pos, haplotag_dict, pileup_dict, hap_dict, max_depth, use_tensor_sample_mode=False):
    """
    Sort by reads haplotype after haplotag reads otherwise sort by read start position.
    center_pos: define the center candidate position for processing.
    haplotag_dict: dictionary (read name : hap type) which keep the read name and haplotype mapping.
    pileup_dict: dictionary (pos: pos info) which keep read information that cover specific position .
    hap_dict: similar to haplotag_dict, dictionary (pos: pos info) which keep the read name and haplotype mapping,
    while haplotype information directly acquire from BAM HP tag.
    platform: select maximum depth for each platform.
    """
    all_nearby_read_name = []
    start_pos, end_pos = center_pos - flanking_base_num, center_pos + flanking_base_num + 1
    for p in range(start_pos, end_pos):
        if p in pileup_dict.keys():
            all_nearby_read_name += pileup_dict[p].read_name_list
    all_nearby_read_name = list(OrderedDict.fromkeys(all_nearby_read_name))  # have sorted by order
    matrix_depth = max_depth
    if len(all_nearby_read_name) > matrix_depth and not use_tensor_sample_mode:
        # set same seed for reproducibility
        random.seed(0)
        indices = random.sample(range(len(all_nearby_read_name)), matrix_depth)
        all_nearby_read_name = [all_nearby_read_name[i] for i in sorted(indices)]
    sorted_read_name_list = []
    for order, read_name in enumerate(all_nearby_read_name):
        hap = max(haplotag_dict[read_name], hap_dict[read_name])  # no phasing is 0
        sorted_read_name_list.append((hap, order, read_name))

    sorted_read_name_list = sorted(sorted_read_name_list)
    return sorted_read_name_list


def get_tensor_info(base_info, bq, ref_base, read_mq=None):
    """
    Create tensor information for each read level position.
    base_info: base information include all alternative bases.
    bq: normalized base quality.
    ref_base: reference_base: upper reference base for cigar calculation.
    read_mq: read mapping quality.
    """

    base, indel = base_info
    ins_base = ""
    query_base = ""
    read_channel = [0] * channel_size
    if base[0] in '*#':
        return read_channel, ins_base, query_base
    strand = STRAND_1
    if base[0] in 'ACGT':
        strand = STRAND_0
    ALT_BASE = 0

    base_upper = base.upper()
    if indel != '':
        ALT_BASE = ACGT_NUM[indel[0]]
    elif (base_upper != ref_base and base_upper in 'ACGT'):
        base_upper = evc_base_from(base_upper)
        ALT_BASE = ACGT_NUM[base_upper]

    REF_BASE = ACGT_NUM[ref_base]
    if len(indel) and indel[0] in '+-':
        if indel[0] == "+":
            ins_base = indel[1:].upper()
    read_channel[:5] = REF_BASE, ALT_BASE, strand, bq, read_mq,
    query_base = "" if base_upper not in "ACGT" else base_upper
    return read_channel, ins_base, query_base


def decode_pileup_bases(pos, pileup_bases, reference_base,  minimum_snp_af_for_candidate, minimum_indel_af_for_candidate, \
                        has_pileup_candidates, candidates_type_dict,is_tumor, mapping_quality, base_quality, base_list=None, platform="ont"):
    """
    Decode mpileup input string.
    pileup_bases: pileup base string for each position, include all mapping information.
    reference_base: upper reference base for cigar calculation.
    pileup_dict: dictionary (pos: pos info) which keep read information that cover specific position.
    ref_seq: chunked reference sequence in window, start: center pos - flankingBaseNum, end: center + flankingBaseNum + 1.
    reference_sequence: reference sequence index by contig:start-end. 0-based.
    minimum_af_for_candidate: default minimum alleic frequency for candidate filtering, filter if below specific thredshold.
    has_pileup_candidates: if the candidate is directly obtained from pileup output, then no need to check the af filtering.
    """

    pileup_tensor = [0] * channel_size
    is_candidate = pos in candidates_type_dict
    if base_list is None:
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

    pileup_dict = defaultdict(int)
    base_counter = Counter([''.join(item) for item, mq in zip(base_list, mapping_quality) if mq >= 20])
    low_mq_base_counter = Counter([''.join(item) for item, mq in zip(base_list, mapping_quality) if mq < 20])
    low_bq_base_counter = Counter([''.join(item) for item, bq in zip(base_list, base_quality) if bq < 10])
    if phasing_info is not None:
        for b, hap in zip(base_list, phasing_info):
            base = ''.join(b)
            if hap in '12' and base in 'ACGTacgt':
                pileup_tensor[channel_size + base_index[base + "HP" + hap]] += 1

    depth, max_ins_0, max_del_0, max_ins_1, max_del_1 = 0, 0, 0, 0, 0
    max_del_length = 0
    alt_info_dict = defaultdict(int)
    for key, count in base_counter.items():
        if len(key) == 1:
            if key.upper() in 'ACGT':
                pileup_dict[key.upper()] += count
                if is_candidate and key.upper() != reference_base:
                    alt_info_dict['X' + key.upper()] += count
                depth += count
                # if key.upper() != reference_base:
                    # alt_dict['X' + key.upper()] += count
                pileup_tensor[BASE2INDEX[key]] += count
            elif key in '#*':
                pileup_tensor[BASE2INDEX[key]] += count
                depth += count
        elif key[1] == '+':
            pileup_dict['I'] += count
            if is_candidate:
                alt_info_dict['I' + key[0].upper() + key[2:].upper()] += count
            # two strand
            if key[0] in 'ACGTN*':
                pileup_tensor[BASE2INDEX["I"]] += count
                max_ins_0 = max(max_ins_0, count)
            else:
                pileup_tensor[BASE2INDEX["i"]] += count
                max_ins_1 = max(max_ins_1, count)
        elif key[1] == '-':
            # del_base = reference_sequence[pos - reference_start + 1: pos - reference_start + len(key[1:]) + 1]
            # alt_dict['D' + del_base] += count
            pileup_dict['D'] += count
            if is_candidate:
                alt_info_dict['D' + key[0].upper() + key[2:].upper()] += count
            max_del_length = max(max_del_length, len(key[1:]))
            # two strand
            if key[0] in 'N*ACGT':
                pileup_tensor[BASE2INDEX["D"]] += count
                max_del_0 = max(max_del_0, count)
            else:
                pileup_tensor[BASE2INDEX["d"]] += count
                max_del_1 = max(max_del_1, count)
    alt_info = str(depth) + '-' + ' '.join(
                [' '.join([item[0], str(item[1])]) for item in alt_info_dict.items()]) + '-'
    pileup_tensor[BASE2INDEX['I1']] = max_ins_0
    pileup_tensor[BASE2INDEX['i1']] = max_ins_1
    pileup_tensor[BASE2INDEX['D1']] = max_del_0
    pileup_tensor[BASE2INDEX['d1']] = max_del_1

    for key, count in low_mq_base_counter.items():
        if key.upper() in 'ACGT':
            pileup_tensor[BASE2INDEX[key+'LMQ']] += count

    for key, count in low_bq_base_counter.items():
        if key.upper() in 'ACGT':
            pileup_tensor[BASE2INDEX[key+'LBQ']] += count

    # pileup_tensor[BASE2INDEX[reference_base]] = -1 * pileup_tensor[BASE2INDEX[reference_base]]
    # pileup_tensor[BASE2INDEX[reference_base.lower()]] = -1 * pileup_tensor[BASE2INDEX[reference_base.lower()]]
    # pileup_tensor[BASE2INDEX[reference_base+'LMQ']] = -1 * pileup_tensor[BASE2INDEX[reference_base+'LMQ']]
    # pileup_tensor[BASE2INDEX[reference_base.lower()+'LMQ']] = -1 * pileup_tensor[BASE2INDEX[reference_base.lower()+'LMQ']]
    # pileup_tensor[BASE2INDEX[reference_base+'LBQ']] = -1 * pileup_tensor[BASE2INDEX[reference_base+'LBQ']]
    # pileup_tensor[BASE2INDEX[reference_base.lower()+'LBQ']] = -1 * pileup_tensor[BASE2INDEX[reference_base.lower()+'LBQ']]

    pileup_tensor[BASE2INDEX[reference_base]] = sum([pileup_tensor[BASE2INDEX[item]] for item in 'ACGT'])
    pileup_tensor[BASE2INDEX[reference_base.lower()]] = sum([pileup_tensor[BASE2INDEX[item]] for item in 'acgt'])
    pileup_tensor[BASE2INDEX[reference_base+'LMQ']] = sum([pileup_tensor[BASE2INDEX[item+'LMQ']] for item in 'ACGT'])
    pileup_tensor[BASE2INDEX[reference_base.lower()+'LMQ']] = sum([pileup_tensor[BASE2INDEX[item+'LMQ']] for item in 'acgt'])
    pileup_tensor[BASE2INDEX[reference_base+'LBQ']] = sum([pileup_tensor[BASE2INDEX[item+'LBQ']] for item in 'ACGT'])
    pileup_tensor[BASE2INDEX[reference_base.lower()+'LBQ']] = sum([pileup_tensor[BASE2INDEX[item+'LBQ']] for item in 'acgt'])


    if has_pileup_candidates:
    #     if pos not in candidates_type_dict or not is_tumor:
        return pileup_tensor, base_list, None, True, 1.0, alt_info

    depth = 0
    for key, count in base_counter.items():
        if key[0].upper() in 'ACGT':
            pileup_dict[key[0].upper()] += count
            depth += count
        elif key[0] in "#*":
            depth += count
        if len(key) > 1 and key[1] == '+':
            pileup_dict['I'] += count
        elif len(key) > 1 and key[1] == '-':
            pileup_dict['D'] += count

    minimum_snp_af_for_candidate = minimum_snp_af_for_candidate if minimum_snp_af_for_candidate > 0 else param.min_af
    minimum_indel_af_for_candidate = minimum_indel_af_for_candidate if minimum_indel_af_for_candidate > 0 else param.min_af_dict[platform]

    denominator = depth if depth > 0 else 1
    pileup_list = sorted(list(pileup_dict.items()), key=lambda x: x[1], reverse=True)

    pass_snp_af = False
    pass_indel_af = False

    for item, count in pileup_list:
        if pass_snp_af or pass_indel_af:
            break
        if item == reference_base:
            continue
        elif item[0] in 'ID':
            pass_indel_af = (pass_indel_af or (float(count) / denominator >= minimum_indel_af_for_candidate))
            continue
        pass_snp_af = pass_snp_af or (float(count) / denominator >= minimum_snp_af_for_candidate)

    af = (float(pileup_list[1][1]) / denominator) if len(pileup_list) > 1 else 0.0
    af = (float(pileup_list[0][1]) / denominator) if len(pileup_list) >= 1 and pileup_list[0][
        0] != reference_base else af

    pass_af = pass_snp_af or pass_indel_af

    return pileup_tensor, base_list, depth, pass_af, af, alt_info


def get_alt_info(center_pos, pileup_dict, ref_seq, reference_sequence, reference_start, hap_dict):
    """
    Get alternative information for representation unification, keep all read level alignment information including phasing info.
    center_pos: center position for processing, default window size = no_of_positions = flankingBaseNum + 1 + flankingBaseNum
    pileup_dict: dictionary (pos: pos info) which keep read information that cover specific position .
    ref_seq: chunked reference sequence in window, start: center pos - flankingBaseNum, end: center + flankingBaseNum + 1.
    reference_sequence: reference sequence index by contig:start-end. 0-based.
    reference_base: upper reference base for cigar calculation.
    reference_start: upper reference base for cigar calculation.
    hap_dict: dictionary (pos: pos info) which keep the read name and haplotype mapping.
    """

    reference_base = ref_seq[flanking_base_num]
    alt_read_name_dict = defaultdict(set)
    depth = 0
    for (base, indel), read_name in zip(pileup_dict[center_pos].base_list, pileup_dict[center_pos].read_name_list):
        if base in "#*":
            alt_read_name_dict['*'].add(read_name)
            depth += 1
            continue
        depth += 1
        if base.upper() == reference_base and indel == '':
            alt_read_name_dict['R'].add(read_name)
        if indel != '':
            if indel[0] == '+':
                indel = 'I' + base.upper() + indel.upper()[1:]
            else:
                del_bases_num = len(indel[1:])
                del_ref_bases = reference_sequence[
                                center_pos - reference_start + 1:center_pos - reference_start + del_bases_num + 1]
                indel = 'D' + del_ref_bases
            alt_read_name_dict[indel].add(read_name)

        if indel == '' and base.upper() != reference_base:
            alt_read_name_dict['X' + base.upper()].add(read_name)

    for alt_type, read_name_set in list(alt_read_name_dict.items()):
        alt_read_name_dict[alt_type] = ' '.join(
            [read_name + '_' + str(hap_dict[read_name]) for read_name in list(read_name_set)])

    alt_info = str(depth) + '\t' + json.dumps(alt_read_name_dict)

    return alt_info

def find_tumor_alt_match(center_pos, sorted_read_name_list, read_name_dict, all_nearby_read_name, truths_variant_dict, proportion=None):
    if proportion is not None and float(proportion) == 1.0:
        # all reads are from tumor reads
        tumor_reads = all_nearby_read_name
        normal_reads = []
    else:
        tumor_reads = [read_name for read_name in all_nearby_read_name if read_name.startswith('t')]
        normal_reads = [read_name for read_name in all_nearby_read_name if read_name.startswith('n')]
    ref_base, alt_base = truths_variant_dict[center_pos].reference_bases, truths_variant_dict[center_pos].alternate_bases[0]
    is_ins = len(alt_base) > 1 and len(ref_base) == 1
    is_del = len(ref_base) > 1 and len(alt_base) == 1
    is_snp = len(ref_base) == 1 and len(alt_base) == 1

    matched_read_name_set = set()
    normal_read_name_set = set(normal_reads)
    tumor_read_name_set = set(tumor_reads)
    for read_name in tumor_reads:
        if read_name in read_name_dict:
            base, indel = read_name_dict[read_name]
            base_upper = base.upper()
            if is_ins and indel[1:].upper() == alt_base:
                matched_read_name_set.add(read_name)
            elif is_del and indel[1:].upper() == ref_base[1:]:
                matched_read_name_set.add(read_name)
            elif is_snp and base_upper == alt_base:
                matched_read_name_set.add(read_name)
    return matched_read_name_set, normal_read_name_set, tumor_read_name_set

def generate_tensor(ctg_name,
                    center_pos,
                    sorted_read_name_list,
                    pileup_dict,
                    ref_seq,
                    reference_sequence,
                    reference_start,
                    platform,
                    confident_bed_tree,
                    add_no_phasing_data_training,
                    is_tumor,
                    candidates_type_dict,
                    use_tensor_sample_mode=False,
                    truths_variant_dict=None):
    """
    Generate full alignment input tensor
    ctg_name: provided contig name.
    center_pos: center position for full alignment generation, default window size = no_of_positions =
    flankingBaseNum + 1 + flankingBaseNum
    sorted_read_name_list: read name list which have been sorted by read start position and haplotype.
    pileup_dict: dictionary (pos: pos info) which keep read information that cover specific position .
    ref_seq: chunked reference sequence in window, start: center pos - flankingBaseNum, end: center + flankingBaseNum + 1.
    reference_sequence: reference sequence index by contig:start-end. 0-based.
    reference_base: upper reference base for cigar calculation.
    reference_start: upper reference base for cigar calculation.
    platform: platform for tensor shape, ont give a larger maximum depth compared with pb and illumina.
    confident_bed_tree: dictionary (contig name : intervaltree) for fast region query.
    add_no_phasing_data_training: boolean option to decide whether add no phasing data in training, we will
    resort the read and remove haplotype info when using this option.
    """

    tensor_shape = param.ont_input_shape if platform == 'ont' else param.input_shape
    reference_base = ref_seq[flanking_base_num]
    tensor_depth = len(sorted_read_name_list)
    if tensor_depth == 0:
        return None, None
    tensor = [[[0] * tensor_shape[2] for _ in range(tensor_shape[1])] for _ in range(tensor_depth)]
    start_pos, end_pos = center_pos - flanking_base_num, center_pos + flanking_base_num + 1
    insert_tuple = []

    # match deletion cases and bed format
    pass_confident_bed = not len(confident_bed_tree) or is_region_in(confident_bed_tree, ctg_name,
                                                                     center_pos - 2,
                                                                     center_pos + flanking_base_num + 1)
    if not pass_confident_bed:
        return None, None

    for p in range(start_pos, end_pos):
        if p in pileup_dict and not pileup_dict[p].update_info:
            pileup_dict[p].update_infos()
        for read_idx, read_name_info in enumerate(sorted_read_name_list):
            hap, read_order, read_name = read_name_info
            offset = p - start_pos
            if p in pileup_dict and read_name in pileup_dict[p].read_info:
                read_channel, ins_base = pileup_dict[p].read_info[read_name]
                tensor[read_idx][offset] = read_channel
                if ins_base != '' and p < end_pos - 1:
                    insert_tuple.append((read_idx, offset, ins_base, p))

    for read_idx, p, ins_base, center_p in insert_tuple:

        for ins_idx in range(min(len(ins_base), no_of_positions - p)):
            tensor[read_idx][ins_idx + p][6] = ACGT_NUM[ins_base[ins_idx]]

    matrix_depth = param.tumor_matrix_depth_dict[platform] if is_tumor else param.normal_matrix_depth_dict[platform]

    min_af_for_samping = 0.05
    max_af_for_sampling = 1.0
    chunk_read_size = 4
    min_tumor_support_read_num = param.min_tumor_support_read_num
    tensor_string_list = []
    alt_info_list = []
    # gradient = each reads
    if use_tensor_sample_mode:

        tumor_reads_meet_alt_info_set, normal_read_name_set, tumor_read_name_set = find_tumor_alt_match(center_pos, sorted_read_name_list, pileup_dict, truths_variant_dict)
        if len(tumor_reads_meet_alt_info_set) == 0:
            print ("No reads support tumor alternative in pos:{}".format(center_pos))
            return None, None

        tumor_read_name_list = list(tumor_read_name_set)
        normal_read_name_list = list(normal_read_name_set)
        tumor_reads_num = len(tumor_read_name_set)
        tumor_reads_meet_alt_info_num = len(tumor_reads_meet_alt_info_set)
        porportion = tumor_reads_meet_alt_info_num / float(tumor_reads_num)
        paired_reads_num = len(normal_read_name_set)
        sampled_reads_num_list = []
        for read_num in range(len(tumor_read_name_set)):
            if read_num == 0 or paired_reads_num == 0 or read_num > matrix_depth:
                continue
            tumor_af = read_num / (read_num + paired_reads_num) * porportion
            if tumor_af >= min_af_for_samping and tumor_af <= max_af_for_sampling:
                sampled_reads_num_list.append(read_num)

        sampled_reads_num_list = sampled_reads_num_list[::chunk_read_size]

        sampled_reads_num_list = [read_num for read_num in sampled_reads_num_list if
                                  min_tumor_support_read_num is None or read_num >= min_tumor_support_read_num]

        for read_num in sampled_reads_num_list:

            sampled_tumor_read_name_list = random.sample(tumor_read_name_list, read_num)
            sampled_tumor_read_name_meet_alt_set = set(sampled_tumor_read_name_list).intersection(tumor_reads_meet_alt_info_set)
            #in training, the candiates should have enough read support
            if len(sampled_tumor_read_name_meet_alt_set) < min_tumor_support_read_num:
                continue

            #check the strand info of sampled_tumor_read_name_list
            forward_strand = sum([1 for rn in sampled_tumor_read_name_meet_alt_set if rn.endswith('1')])
            if forward_strand == 0 or forward_strand == len(sampled_tumor_read_name_meet_alt_set):
                continue
            sampled_normal_read_name_list = normal_read_name_list
            if len(normal_read_name_list) + read_num > matrix_depth:
                # set same seed for reproducibility
                random.seed(0)
                # print(len(normal_read_name_list), matrix_depth, read_num)
                sampled_normal_read_name_list = random.sample(normal_read_name_list, matrix_depth - read_num)
            re_sorted_read_name_list = sampled_normal_read_name_list + sampled_tumor_read_name_list
            tmp_tensor = []
            re_sorted_read_name_set = set(re_sorted_read_name_list)

            tmp_depth = len(re_sorted_read_name_set)
            af_set = set()
            tmp_read_name_list = []
            for row_idx, (hap, _, read_name) in enumerate(sorted_read_name_list):
                if read_name in re_sorted_read_name_set:
                    tmp_tensor.append(tensor[row_idx])
                    tmp_read_name_list.append(read_name)
            alt_dict = defaultdict(int)
            for read_name, (base, indel) in zip(pileup_dict[center_pos].read_name_list,
                                                pileup_dict[center_pos].base_list):
                if base in "#*":
                    continue
                if read_name not in re_sorted_read_name_set:
                    continue
                base_upper = base.upper()
                if indel != '':
                    if indel[0] == '+':
                        alt_dict['+' + base_upper + indel[1:].upper()] += 1
                    else:  # del
                        alt_dict[indel.upper()] += 1
                elif base.upper() != reference_base:
                    alt_dict[base.upper()] += 1

            for row_idx, read_name in enumerate(tmp_read_name_list):
                af_num = 0
                if read_name in pileup_dict[center_pos].read_name_dict:
                    base, indel = pileup_dict[center_pos].read_name_dict[read_name]
                    base_upper = base.upper()
                    if indel != '':
                        if indel[0] == '+':
                            insert_str = ('+' + base_upper + indel.upper()[1:])
                            af_num = alt_dict[insert_str] / max(1, float(tmp_depth)) if insert_str in alt_dict else af_num
                        else:
                            af_num = alt_dict[indel.upper()] / max(1, float(tmp_depth)) if indel.upper() in alt_dict else af_num
                    elif base.upper() in alt_dict:
                        af_num = alt_dict[base_upper] / max(1, float(tmp_depth))
                af_num = _normalize_af(af_num) if af_num != 0 else af_num
                af_set.add(round(af_num / 100, 3))
                # hap_type = HAP_TYPE[hap]
                hap_type = 100 if is_tumor else 50
                for p in range(no_of_positions):
                    if tmp_tensor[row_idx][p][2] != 0:  # skip all del #*
                        tmp_tensor[row_idx][p][5] = af_num
                        tmp_tensor[row_idx][p][7] = hap_type

            # print (len(tmp_tensor))
            alt_info = []
            af_infos = ' '.join([str(item) for item in sorted(list(af_set), reverse=True) if item != 0])
            for alt_type, alt_count in alt_dict.items():
                if alt_type[0] == '+':
                    alt_info.append(['I' + alt_type[1:].upper(), str(alt_count)])
                elif alt_type[0] == '-':
                    del_bases_num = len(alt_type[1:])
                    del_ref_bases = reference_sequence[
                                    center_pos - reference_start + 1:center_pos - reference_start + del_bases_num + 1]
                    alt_info.append(['D' + del_ref_bases, str(alt_count)])
                else:
                    alt_info.append(['X' + alt_type, str(alt_count)])
            alt_info = str(tmp_depth) + '-' + ' '.join(
                [' '.join([item[0], str(item[1])]) for item in alt_info]) + '-' + af_infos
            alt_info_list.append(alt_info)
            tensor_string_list.append(" ".join(
                (" ".join(" ".join(str(x) for x in innerlist) for innerlist in outerlist)) for outerlist in tmp_tensor))
            variant_type = candidates_type_dict[center_pos] if center_pos in candidates_type_dict else 'unknown'

        if 0:
            import numpy as np
            tmp_list = [item.split(' ') for item in tensor_string_list]
            tmp = [np.array(tmp, dtype=int).reshape((-1, 33, 7)) for tmp in tmp_list]

        return '\n'.join(["%s\t%d\t%s\t%s\t%s\t%s\t%s" % (
            ctg_name,
            center_pos,
            ref_seq,
            tensor_string,
            alt_info,
            "tumor" if is_tumor else "normal",
            variant_type
        ) for tensor_string, alt_info in zip(tensor_string_list, alt_info_list)]), ""

    else:
        alt_dict = defaultdict(int)
        depth, max_del_length = 0, 0
        for base, indel in pileup_dict[center_pos].base_list:
            if base in "#*":
                depth += 1
                continue
            depth += 1
            base_upper = base.upper()
            if indel != '':
                if indel[0] == '+':
                    alt_dict['+' + base_upper + indel[1:].upper()] += 1
                else:  # del
                    alt_dict[indel.upper()] += 1
                    max_del_length = max(len(indel), max_del_length)
            elif base.upper() != reference_base:
                alt_dict[base.upper()] += 1

        af_set = set()
        for row_idx, (hap, _, read_name) in enumerate(sorted_read_name_list):
            af_num = 0
            if read_name in pileup_dict[center_pos].read_name_dict:
                base, indel = pileup_dict[center_pos].read_name_dict[read_name]
                base_upper = base.upper()
                if indel != '':
                    if indel[0] == '+':
                        insert_str = ('+' + base_upper + indel.upper()[1:])
                        af_num = alt_dict[insert_str] / max(1, float(depth)) if insert_str in alt_dict else af_num
                    else:
                        af_num = alt_dict[indel.upper()] / max(1, float(depth)) if indel.upper() in alt_dict else af_num
                elif base.upper() in alt_dict:
                    af_num = alt_dict[base_upper] / max(1, float(depth))
            af_num = _normalize_af(af_num) if af_num != 0 else af_num
            af_set.add(round(af_num/100, 3))
            # hap_type = HAP_TYPE[hap]
            hap_type = 100 if is_tumor else 50
            for p in range(no_of_positions):
                if tensor[row_idx][p][2] != 0:  # skip all del #*
                    tensor[row_idx][p][5] = af_num
                    tensor[row_idx][p][7] = hap_type


        alt_info = []
        af_infos = ' '.join([str(item) for item in sorted(list(af_set), reverse=True) if item != 0])
        for alt_type, alt_count in alt_dict.items():
            if alt_type[0] == '+':
                alt_info.append(['I' + alt_type[1:].upper(), str(alt_count)])
            elif alt_type[0] == '-':
                del_bases_num = len(alt_type[1:])
                del_ref_bases = reference_sequence[
                                center_pos - reference_start + 1:center_pos - reference_start + del_bases_num + 1]
                alt_info.append(['D' + del_ref_bases, str(alt_count)])
            else:
                alt_info.append(['X' + alt_type, str(alt_count)])
        alt_info = str(depth) + '-' + ' '.join([' '.join([item[0], str(item[1])]) for item in alt_info]) + '-' + af_infos
        tensor_string_list = [" ".join((" ".join(" ".join(str(x) for x in innerlist) for innerlist in outerlist)) for outerlist in tensor)]
        variant_type = candidates_type_dict[center_pos] if center_pos in candidates_type_dict else 'unknown'

        return '\n'.join(["%s\t%d\t%s\t%s\t%s\t%s\t%s" % (
            ctg_name,
            center_pos,
            ref_seq,
            tensor_string,
            alt_info,
            "tumor" if is_tumor else "normal",
            variant_type
        ) for tensor_string in tensor_string_list]), alt_info




class TensorStdout(object):
    def __init__(self, handle):
        self.stdin = handle

    def __del__(self):
        self.stdin.close()


def update_hetero_ref(pos, reference_sequence, reference_start, extend_bp, alt_base):
    # if need phasing option enables, will store reference sequence near hetero snp candidate.
    ref_start = pos - extend_bp
    ref_end = pos + extend_bp + 1
    ref_seq = reference_sequence[ref_start - reference_start: ref_end - reference_start]
    alt_seq = ref_seq[:extend_bp] + alt_base + ref_seq[extend_bp + 1:]
    return ref_seq, alt_seq

def get_normal_set(alt_fn):
    normal_pos_set = set()
    file_path_process = subprocess_popen(shlex.split("gzip -fdc %s" % (alt_fn)))
    file_path_output = file_path_process.stdout
    for row in file_path_output:
        ctg_name, pos = row.rstrip().split(maxsplit=2)
        normal_pos_set.add(int(pos))
    file_path_output.close()
    file_path_process.wait()
    return normal_pos_set

def create_tensor(args):
    ctg_start = args.ctg_start
    ctg_end = args.ctg_end
    candidates_bed_regions = args.candidates_bed_regions
    fasta_file_path = args.ref_fn
    ctg_name = args.ctg_name
    need_phasing = args.need_phasing
    samtools_execute_command = args.samtools
    bam_file_path = args.bam_fn
    chunk_id = args.chunk_id - 1 if args.chunk_id else None  # 1-base to 0-base
    chunk_num = args.chunk_num
    tensor_can_output_path = args.tensor_can_fn
    is_candidates_bed_regions_given = candidates_bed_regions is not None
    phasing_info_in_bam = args.phasing_info_in_bam
    phasing_window_size = args.phasing_window_size
    extend_bp = param.extend_bp
    # unify_repre = args.unify_repre
    # minimum_af_for_candidate = args.min_af
    minimum_snp_af_for_candidate = args.snp_min_af
    minimum_indel_af_for_candidate = args.indel_min_af
    min_coverage = args.min_coverage
    platform = args.platform
    confident_bed_fn = args.bed_fn
    alt_fn = args.alt_fn
    extend_bed = args.extend_bed
    is_extend_bed_file_given = extend_bed is not None
    min_mapping_quality = args.min_mq
    min_base_quality = args.min_bq
    # unify_repre_fn = args.unify_repre_fn
    vcf_fn = args.vcf_fn
    is_known_vcf_file_provided = vcf_fn is not None
    tensor_sample_mode = args.tensor_sample_mode
    global test_pos
    test_pos = None
    hetero_snp_pos_dict = defaultdict()
    hetero_snp_tree = IntervalTree()
    candidates_pos_set = set()
    candidates_type_dict = defaultdict(str)
    add_read_regions = True
    training_mode = args.training_mode
    truth_vcf_fn = args.truth_vcf_fn
    is_truth_vcf_provided = truth_vcf_fn is not None
    truths_variant_dict = {}
    proportion = args.proportion
    if is_truth_vcf_provided:
        from shared.vcf import VcfReader
        unified_vcf_reader = VcfReader(vcf_fn=truth_vcf_fn, ctg_name=ctg_name, is_var_format=False)
        unified_vcf_reader.read_vcf()
        truths_variant_dict = unified_vcf_reader.variant_dict

    if candidates_bed_regions:

        """
        If given full alignment bed regions, all candidate positions will be directly selected from each row, define as 
        'ctg start end', where 0-based center position is the candidate for full alignment calling.
        if 'need_phasing' option enables, full alignment bed regions will also include nearby heterozygous snp candidates for reads
        haplotag, which is faster than whatshap haplotag with more memory occupation.
        """

        candidate_file_path_process = subprocess_popen(shlex.split("gzip -fdc %s" % (candidates_bed_regions)))
        candidate_file_path_output = candidate_file_path_process.stdout

        ctg_start, ctg_end = float('inf'), 0
        for row in candidate_file_path_output:
            row = row.rstrip().split('\t')
            if row[0] != ctg_name: continue
            position = int(row[1]) + 1
            end = int(row[2]) + 1
            ctg_start = min(position, ctg_start)
            ctg_end = max(end, ctg_end)

            # if platform == "ilmn":
            #     continue
            # if len(row) > 3:  # hetero snp positions
            #     center_pos = position + extend_bp + 1
            #     ref_base, alt_base, genotype, phase_set = row[3].split('-')
            #     hetero_snp_pos_dict[center_pos] = Position(pos=center_pos, ref_base=ref_base, alt_base=alt_base,
            #                                              genotype=int(genotype), phase_set=phase_set)
            #     hetero_snp_tree.addi(begin=center_pos - extend_bp, end=center_pos + extend_bp + 1)
            # else:
            center = position + (end - position) // 2 - 1
            candidates_pos_set.add(center)
            variant_type = 'unknown'
            if len(row) == 4:
                variant_type = row[3]
            # if variant_type == 'hem'
            candidates_type_dict[center] = variant_type
        candidate_file_path_output.close()
        candidate_file_path_process.wait()

        # currently deprecate using ctg_name.start_end as file name, which will run similar regions for several times when start and end has slight difference
        # if '.' in candidates_bed_regions.split('/')[-1] and len(candidates_bed_regions.split('/')[-1].split('.')[-1].split('_')) > 0:
        #     ctg_start, ctg_end = candidates_bed_regions.split('/')[-1].split('.')[-1].split('_')
        #     ctg_start, ctg_end = int(ctg_start), int(ctg_end)
    # if platform == 'ilmn' and bam_file_path == "PIPE":
    #     add_read_regions = False

    fai_fn = file_path_from(fasta_file_path, suffix=".fai", exit_on_not_found=True, sep='.')

    if is_known_vcf_file_provided:
        known_variants_list = vcf_candidates_from(vcf_fn=vcf_fn, contig_name=ctg_name)
        known_variants_set = set(known_variants_list)
    if not candidates_bed_regions and chunk_id is not None:

        """
        Whole genome calling option, acquire contig start end position from reference fasta index(.fai), then split the
        reference accroding to chunk id and total chunk numbers.
        """
        contig_length = 0
        with open(fai_fn, 'r') as fai_fp:
            for row in fai_fp:
                columns = row.strip().split("\t")

                contig_name = columns[0]
                if contig_name != ctg_name:
                    continue
                contig_length = int(columns[1])
        chunk_size = contig_length // chunk_num + 1 if contig_length % chunk_num else contig_length // chunk_num
        ctg_start = chunk_size * chunk_id  # 0-base to 1-base
        ctg_end = ctg_start + chunk_size

        # for illumina platform, the reads alignment is acquired after reads realignment from ReadsRealign.py
        if bam_file_path != "PIPE":
            bam_file_path += '.{}_{}'.format(ctg_start, ctg_end)
            add_read_regions = False
        if bam_file_path == "PIPE":
            add_read_regions = False

    # preparation for candidates near variants
    candidates_pos_set = set([item for item in candidates_pos_set if item >= ctg_start and item <= ctg_end])
    # 1-based regions [start, end] (start and end inclusive)
    ref_regions = []
    reads_regions = []

    is_ctg_name_given = ctg_name is not None
    is_ctg_range_given = is_ctg_name_given and ctg_start is not None and ctg_end is not None
    extend_start, extend_end = None, None
    if is_ctg_range_given:
        extend_start = ctg_start - (phasing_window_size if need_phasing else no_of_positions)
        extend_end = ctg_end + (phasing_window_size if need_phasing else no_of_positions)
        reads_regions.append(region_from(ctg_name=ctg_name, ctg_start=extend_start, ctg_end=extend_end))
        reference_start, reference_end = ctg_start - param.expandReferenceRegion, ctg_end + param.expandReferenceRegion
        reference_start = 1 if reference_start < 1 else reference_start
        ref_regions.append(region_from(ctg_name=ctg_name, ctg_start=reference_start, ctg_end=reference_end))
    elif is_ctg_name_given:
        reads_regions.append(region_from(ctg_name=ctg_name))
        ref_regions.append(region_from(ctg_name=ctg_name))
        reference_start = 1

    reference_sequence = reference_sequence_from(
        samtools_execute_command=samtools_execute_command,
        fasta_file_path=fasta_file_path,
        regions=ref_regions
    )
    if reference_sequence is None or len(reference_sequence) == 0:
        sys.exit("[ERROR] Failed to load reference sequence from file ({}).".format(fasta_file_path))

    phasing_option = " --output-extra HP" if phasing_info_in_bam else " "
    samtools_view_min_mq = 0
    # mq_option = ' --min-MQ {}'.format(min_mapping_quality)
    mq_option = ' --min-MQ {}'.format(samtools_view_min_mq)
    output_mq, output_read_name = True, training_mode
    output_mq_option = ' --output-MQ ' if output_mq else ""
    output_read_name_option = ' --output-QNAME ' if output_read_name else ""
    bq_option = ' --min-BQ {}'.format(min_base_quality)
    # pileup bed first
    bed_option = ' -l {}'.format(
        extend_bed) if is_extend_bed_file_given else ""
    bed_option = ' -l {}'.format(candidates_bed_regions) if is_candidates_bed_regions_given else bed_option
    flags_option = ' --excl-flags {} '.format(param.SAMTOOLS_VIEW_FILTER_FLAG)
    max_depth_option = ' --max-depth {}'.format(args.max_depth) if args.max_depth is not None else " "
    reads_regions_option = ' -r {}'.format(" ".join(reads_regions)) if add_read_regions else ""
    # print (add_read_regions, ctg_start, ctg_end, reference_start)
    stdin = None if bam_file_path != "PIPE" else sys.stdin
    bam_file_path = bam_file_path if bam_file_path != "PIPE" else "-"
    samtools_command = "{} mpileup {} --reverse-del".format(samtools_execute_command, bam_file_path) + \
                       output_read_name_option + output_mq_option + reads_regions_option + phasing_option + mq_option + bq_option + bed_option + flags_option + max_depth_option
    samtools_mpileup_process = subprocess_popen(
        shlex.split(samtools_command), stdin=stdin)

    is_tumor = "tumor_" in bam_file_path
    # normal_pos_set = get_normal_set(alt_fn) if is_tumor and alt_fn is not None else None
    # alt_fn = None if is_tumor else alt_fn

    if tensor_can_output_path != "PIPE":
        tensor_can_fpo = open(tensor_can_output_path, "wb")
        tensor_can_fp = subprocess_popen(shlex.split("{} -c".format(args.zstd)), stdin=PIPE, stdout=tensor_can_fpo)
    else:
        tensor_can_fp = TensorStdout(sys.stdout)

    # if unify_repre_fn:
    #     label_fp = open(unify_repre_fn, 'w')
    if alt_fn:
        output_alt_fn = alt_fn
        alt_fpo = open(output_alt_fn, "wb")
        alt_fp = subprocess_popen(shlex.split("{} -c".format(args.zstd)), stdin=PIPE, stdout=alt_fpo)

    hap_dict = defaultdict(int)
    haplotag_dict = defaultdict(int)
    pileup_dict = defaultdict(str)
    extend_bp_distance = phasing_window_size if need_phasing else no_of_positions + param.extend_bp
    # confident_bed_tree = bed_tree_from(bed_file_path=confident_bed_fn,
    #                                    contig_name=ctg_name,
    #                                    bed_ctg_start=extend_start,
    #                                    bed_ctg_end=extend_end)

    extend_bed_tree = bed_tree_from(bed_file_path=extend_bed,
                                    contig_name=ctg_name,
                                    bed_ctg_start=extend_start,
                                    bed_ctg_end=extend_end)

    pileup_tensors = [[0] * channel_size] * (extend_end - extend_start)

    alt_info_dict = defaultdict()
    pileup_info_dict = defaultdict()
    need_phasing_pos_list = sorted(list(candidates_pos_set))
    has_pileup_candidates = len(candidates_pos_set)
    use_alt_base = False if platform == 'ont' else param.use_alt_base

    def samtools_pileup_generator_from(samtools_mpileup_process):
        current_pos_index = 0
        for row in samtools_mpileup_process.stdout:  # chr position N depth seq BQ read_name mapping_quality phasing_info
            columns = row.strip().split('\t')
            pos = int(columns[1])
            # pos that near bed region should include some indel cover in bed
            pass_extend_bed = not is_extend_bed_file_given or is_region_in(extend_bed_tree,
                                                                                     ctg_name, pos - 1,
                                                                                     pos + 1)
            pass_ctg_range = not ctg_start or (pos >= ctg_start and pos <= ctg_end)
            if not has_pileup_candidates and not pass_extend_bed and pass_ctg_range:
                continue
            pileup_bases = columns[4]
            raw_base_quality = columns[5]
            raw_mapping_quality = columns[6]
            read_name_list = columns[7].split(',') if training_mode else []
            reference_base = reference_sequence[pos - reference_start].upper()
            if reference_base not in 'ACGT':
                continue

            mapping_quality = [ord(mq) - 33 for mq in raw_mapping_quality]
            base_quality = [ord(mq) - 33 for mq in raw_base_quality]

            pileup_tensor, base_list, depth, pass_af, af, alt_info = decode_pileup_bases(pos=pos,
                                                                pileup_bases=pileup_bases,
                                                                reference_base=reference_base,
                                                                minimum_snp_af_for_candidate=minimum_snp_af_for_candidate,
                                                                minimum_indel_af_for_candidate=minimum_indel_af_for_candidate,
                                                                has_pileup_candidates=has_pileup_candidates,
                                                                candidates_type_dict=candidates_type_dict,
                                                                mapping_quality=mapping_quality,
                                                                base_quality=base_quality,
                                                                # use_tensor_sample_mode=use_tensor_sample_mode,
                                                                is_tumor=is_tumor)
            if training_mode:
                for b_idx, base in enumerate(base_list):
                    if base[0] == '#' or (base[0] >= 'a' and base[0] <= 'z'):
                        read_name_list[b_idx] += '_1'  # reverse
                    else:
                        read_name_list[b_idx] += '_0'  # forward

                pileup_info_dict[pos] = (base_list, read_name_list, mapping_quality, base_quality)
            # if len(read_name_list) != len(base_list):
            #     continue
            offset = pos - extend_start
            pileup_tensors[offset] = pileup_tensor
            if pos in candidates_type_dict:
                alt_info_dict[pos] = alt_info

            if not is_known_vcf_file_provided and not has_pileup_candidates and reference_base in 'ACGT' and (
                    pass_af and depth >= min_coverage):
                need_phasing_pos_list.append(pos)

            if is_known_vcf_file_provided and not has_pileup_candidates and pos in known_variants_set:
                need_phasing_pos_list.append(pos)

            # for b_idx, base in enumerate(base_list):
            #     if base[0] == '#' or (base[0] >= 'a' and base[0] <= 'z'):
            #         read_name_list[b_idx] += '_1' # reverse
            #     else:
            #         read_name_list[b_idx] += '_0' # forward

            # pileup_dict[pos] = Position(pos=pos,
            #                             ref_base=reference_base,
            #                             # read_name_list=read_name_list,
            #                             base_list=base_list,
            #                             raw_base_quality=raw_base_quality,
            #                             raw_mapping_quality=raw_mapping_quality,
            #                             af=af,
            #                             depth=depth)

            # overlap_hetero_region = hetero_snp_tree.at(pos)

            if current_pos_index < len(need_phasing_pos_list) and pos - need_phasing_pos_list[
                current_pos_index] > extend_bp_distance:
                yield need_phasing_pos_list[current_pos_index]
                # for pre_pos in sorted(pileup_dict.keys()):
                #     if need_phasing_pos_list[current_pos_index] - pre_pos > extend_bp_distance:
                #         del pileup_dict[pre_pos]
                #     else:
                #         break
                current_pos_index += 1
        while current_pos_index != len(need_phasing_pos_list):
            yield need_phasing_pos_list[current_pos_index]
            # for pre_pos in sorted(pileup_dict.keys()):
            #     if need_phasing_pos_list[current_pos_index] - pre_pos > extend_bp_distance:
            #         del pileup_dict[pre_pos]
            #     else:
            #         break
            current_pos_index += 1

    samtools_pileup_generator = samtools_pileup_generator_from(samtools_mpileup_process)

    for pos in samtools_pileup_generator:
        # if pos not in pileup_dict:
        #     continue

        # max_depth = param.tumor_matrix_depth_dict[platform] if is_tumor else param.normal_matrix_depth_dict[platform]
        # sorted_read_name_list = sorted_by_hap_read_name(pos, haplotag_dict, pileup_dict, hap_dict, max_depth, use_tensor_sample_mode)
        ref_seq = reference_sequence[
                  pos - reference_start - flanking_base_num: pos - reference_start + flanking_base_num + 1].upper()

        start_index = pos - flanking_base_num - extend_start
        end_index = start_index + no_of_positions
        if start_index < 0 or end_index >= extend_end - extend_start:
            continue
        if sum([1 for tensor in pileup_tensors[start_index:end_index] if len(tensor) == 0]) > 0:
            continue

        use_tensor_sample_mode = tensor_sample_mode and (
                candidates_type_dict[pos] == 'homo_somatic' or candidates_type_dict[
            pos] == 'hetero_somatic') and pos in truths_variant_dict and training_mode

        tensor_string_list = []
        alt_info_list = []
        if pos not in alt_info_dict:
            continue
        if use_tensor_sample_mode:
            base_list, read_name_list = pileup_info_dict[pos][0], pileup_info_dict[pos][1]
            read_name_dict = dict(zip(read_name_list, base_list))
            center_pos = pos
            all_nearby_read_name = []
            start_pos, end_pos = center_pos - flanking_base_num, center_pos + flanking_base_num + 1
            for p in range(start_pos, end_pos):
                if p in pileup_info_dict:
                    all_nearby_read_name += pileup_info_dict[p][1]
            all_nearby_read_name = list(OrderedDict.fromkeys(all_nearby_read_name))  # have sorted by order
            tumor_reads_meet_alt_info_set, normal_read_name_set, tumor_read_name_set = find_tumor_alt_match(center_pos,
                                                                                                            read_name_list,
                                                                                                            read_name_dict,
                                                                                                            all_nearby_read_name,
                                                                                                            truths_variant_dict,
                                                                                                            proportion=proportion)

            if len(tumor_reads_meet_alt_info_set) == 0:
                print("No reads support tumor alternative in pos:{}".format(center_pos))
                continue
            min_af_for_samping = 0.03125
            max_af_for_sampling = 1.0
            chunk_read_size = 4
            min_tumor_support_read_num = param.min_tumor_support_read_num
            tumor_read_name_list = list(tumor_read_name_set)
            normal_read_name_list = list(normal_read_name_set)
            tumor_reads_num = len(tumor_read_name_set)
            tumor_reads_meet_alt_info_num = len(tumor_reads_meet_alt_info_set)
            tumor_read_porportion = tumor_reads_meet_alt_info_num / float(tumor_reads_num)
            paired_reads_num = len(normal_read_name_set)
            sampled_reads_num_list = []

            partition = 3
            if param.use_beta_subsampling:
                beta_acc_per = param.beta_acc_per
                sampled_reads_num_list.append(len(tumor_read_name_list))
                for s_idx in range(partition):
                    random.seed(pos + s_idx)
                    random_af = random.random()
                    af = None
                    for pro_idx in range(len(beta_acc_per) - 1):
                        if random_af >= beta_acc_per[pro_idx] and random_af < beta_acc_per[pro_idx + 1]:
                            af = pro_idx / 100.0
                            break
                    if af == None:
                        continue

                    sampled_read_num = int((len(tumor_read_name_set) * af) / tumor_read_porportion)
                    if sampled_read_num >= min_tumor_support_read_num and sampled_read_num <= len(tumor_read_name_set):
                        sampled_reads_num_list.append(sampled_read_num)

            elif param.use_exp_subsampling:
                for p in range(1, partition):
                    af = 1 / float(2 ** p)
                    sampled_read_num = int((len(tumor_read_name_set) * af ) / tumor_read_porportion)
                    if sampled_read_num >= min_tumor_support_read_num and sampled_read_num <= len(tumor_read_name_set):
                        sampled_reads_num_list.append(sampled_read_num)

            #
            # for read_num in range(len(tumor_read_name_set)):
            #     if read_num == 0:
            #         continue
            #     tumor_af = read_num / (read_num + paired_reads_num) * tumor_read_porportion
            #     if tumor_af >= min_af_for_samping and tumor_af <= max_af_for_sampling and read_num >= min_tumor_support_read_num:
            #         sampled_reads_num_list.append(read_num)

            # sampled_reads_num_list = sampled_reads_num_list[::chunk_read_size]
            # tumor_reads_meet_alt_info_list = list(tumor_reads_meet_alt_info_set)

            for read_num in sampled_reads_num_list:
                random.seed(len(tumor_read_name_list) + read_num)
                sampled_tumor_read_name_list = random.sample(tumor_read_name_list, read_num)
                sampled_tumor_read_name_meet_alt_set = set(sampled_tumor_read_name_list).intersection(
                    tumor_reads_meet_alt_info_set)
                # in training mode, the candidates should have enough read support
                if len(sampled_tumor_read_name_meet_alt_set) < min_tumor_support_read_num:
                    continue

                if len(sampled_tumor_read_name_list) + len(normal_read_name_list) < len(all_nearby_read_name) * 0.3 and not use_alt_base:
                    continue

                # check the strand info of sampled_tumor_read_name_list
                forward_strand = sum([1 for rn in sampled_tumor_read_name_meet_alt_set if rn.endswith('1')])
                if forward_strand == 0 or forward_strand == len(sampled_tumor_read_name_meet_alt_set):
                    continue

                if use_alt_base:
                    sorted_read_name_set = set(normal_read_name_list + tumor_read_name_list)
                else:
                    sorted_read_name_set = set(normal_read_name_list + sampled_tumor_read_name_list)
                tmp_pileup_tensors = []
                tmp_alt_info = []
                for p in range(start_pos, end_pos):
                    pre_base_list, pre_read_name_list, pre_mapping_quality, pre_base_quality = pileup_info_dict[p]
                    base_list, read_name_list, mapping_quality, base_quality = [], [], [], []
                    for base, read_name, mq, bq in zip(pre_base_list, pre_read_name_list, pre_mapping_quality, pre_base_quality):
                        if read_name in sorted_read_name_set:
                            if use_alt_base and p == pos and read_name not in sampled_tumor_read_name_meet_alt_set:
                                if base[0].upper() == truths_variant_dict[pos].alternate_bases[0]:
                                    base = [ref_seq[flanking_base_num] if base[0] in "ACGT" else ref_seq[flanking_base_num].lower(), ""]
                            base_list.append(base)
                            read_name_list.append(read_name)
                            mapping_quality.append(mq)
                            base_quality.append(bq)

                    reference_base = reference_sequence[p - reference_start].upper()
                    pileup_tensor, base_list, depth, pass_af, af, alt_info = decode_pileup_bases(pos=p,
                                                                                                 pileup_bases="",
                                                                                                 reference_base=reference_base,
                                                                                                 minimum_snp_af_for_candidate=minimum_snp_af_for_candidate,
                                                                                                 minimum_indel_af_for_candidate=minimum_indel_af_for_candidate,
                                                                                                 has_pileup_candidates=has_pileup_candidates,
                                                                                                 candidates_type_dict=candidates_type_dict,
                                                                                                 mapping_quality=mapping_quality,
                                                                                                 base_quality=base_quality,
                                                                                                 base_list=base_list,
                                                                                                 is_tumor=is_tumor)
                    tmp_pileup_tensors.append(pileup_tensor)
                    if p == pos:
                        tmp_alt_info = alt_info
                if sum([1 for tensor in tmp_pileup_tensors if len(tensor) == 0]) > 0:
                    continue

                tensor_string_list.append(" ".join(" ".join("%d" % x for x in innerlist) for innerlist in tmp_pileup_tensors))
                alt_info_list.append(tmp_alt_info)
                if 0:
                    import numpy as np
                    a = np.array(tmp_pileup_tensors)
                    b = np.array(pileup_tensors[start_index:end_index])
        else:
            tensor_string_list.append(" ".join(" ".join("%d" % x for x in innerlist) for innerlist in pileup_tensors[start_index:end_index]))
            alt_info_list.append(alt_info_dict[pos])
        if len(tensor_string_list) > 0:
            variant_type = candidates_type_dict[pos] if pos in candidates_type_dict else 'unknown'
            tensor = '\n'.join(["%s\t%d\t%s\t%s\t%s\t%s\t%s" % (
                ctg_name,
                pos,
                ref_seq,
                tensor_string,
                alt_info,
                "tumor" if is_tumor else "normal",
                variant_type
            ) for tensor_string, alt_info in zip(tensor_string_list, alt_info_list)])
            tensor_can_fp.stdin.write(tensor+"\n")
            if 0:
                import numpy as np
                a = []
                for tensor_str in tensor_string_list:
                    a += tensor_str.split(' ')
                a = np.array(a, dtype=float).reshape((-1, no_of_positions, channel_size ))
        # if not unify_repre:
        #     tensor, alt_info = generate_tensor(ctg_name=ctg_name,
        #                                        center_pos=pos,
        #                                        sorted_read_name_list=sorted_read_name_list,
        #                                        pileup_dict=pileup_dict,
        #                                        ref_seq=ref_seq,
        #                                        reference_sequence=reference_sequence,
        #                                        reference_start=reference_start,
        #                                        platform=platform,
        #                                        confident_bed_tree=confident_bed_tree,
        #                                        add_no_phasing_data_training=add_no_phasing_data_training,
        #                                        is_tumor=is_tumor,
        #                                        candidates_type_dict=candidates_type_dict,
        #                                        use_tensor_sample_mode=use_tensor_sample_mode,
        #                                        truths_variant_dict=truths_variant_dict)
        #     if not tensor:
        #         continue
        #
        #     tensor_can_fp.stdin.write(tensor)
        #     tensor_can_fp.stdin.write("\n")
        #     if alt_fn:
        #         # alt_info = alt_info.replace('-', '\t')
        #         alt_fp.stdin.write('\t'.join([ctg_name + ' ' + str(pos), alt_info]) + '\n')

        if unify_repre and unify_repre_fn:
            label_info = get_alt_info(center_pos=pos,
                                      pileup_dict=pileup_dict,
                                      ref_seq=ref_seq,
                                      reference_sequence=reference_sequence,
                                      reference_start=reference_start,
                                      hap_dict=hap_dict)
            label_fp.write('\t'.join([ctg_name + ' ' + str(pos), label_info]) + '\n')

    samtools_mpileup_process.stdout.close()
    samtools_mpileup_process.wait()

    if tensor_can_output_path != "PIPE":
        tensor_can_fp.stdin.close()
        tensor_can_fp.wait()
        tensor_can_fpo.close()

    if alt_fn:
        alt_fp.stdin.close()
        alt_fp.wait()
        alt_fpo.close()

    # if unify_repre_fn:
    #     label_fp.close()


def main():
    parser = ArgumentParser(description="Generate variant candidate tensors")

    parser.add_argument('--platform', type=str, default='ont',
                        help="Sequencing platform of the input, default: %(default)s")

    parser.add_argument('--bam_fn', type=str, default="input.bam",
                        help="Sorted BAM file input, required")

    parser.add_argument('--ref_fn', type=str, default="ref.fa",
                        help="Reference fasta file input, required")

    parser.add_argument('--tensor_can_fn', type=str, default="PIPE",
                        help="Tensor output, stdout by default, default: %(default)s")

    parser.add_argument('--vcf_fn', type=str, default=None,
                        help="Candidate sites VCF file input, if provided, variants will only be called at the sites in the VCF file,  default: %(default)s")

    parser.add_argument('--snp_min_af', type=float, default=0.1,
                        help="Minimum snp allele frequency for a site to be considered as a candidate site, default: %(default)f")

    parser.add_argument('--ctg_name', type=str, default=None,
                        help="The name of sequence to be processed, required if --bed_fn is not defined")

    parser.add_argument('--ctg_start', type=int, default=None,
                        help="The 1-based starting position of the sequence to be processed, optional, will process the whole --ctg_name if not set")

    parser.add_argument('--ctg_end', type=int, default=None,
                        help="The 1-based inclusive ending position of the sequence to be processed, optional, will process the whole --ctg_name if not set")

    parser.add_argument('--bed_fn', type=str, default=None,
                        help="Call variant only in the provided regions. Will take an intersection if --ctg_name and/or (--ctg_start, --ctg_end) are set")

    parser.add_argument('--samtools', type=str, default="samtools",
                        help="Path to the 'samtools', samtools version >= 1.10 is required. default: %(default)s")

    # options for advanced users
    parser.add_argument('--min_coverage', type=float, default=param.min_coverage,
                        help="EXPERIMENTAL: Minimum coverage required to call a variant, default: %(default)f")

    parser.add_argument('--min_mq', type=int, default=param.min_mq,
                        help="EXPERIMENTAL: If set, reads with mapping quality with <$min_mq are filtered, default: %(default)d")

    parser.add_argument('--min_bq', type=int, default=param.min_bq,
                        help="EXPERIMENTAL: If set, bases with base quality with <$min_bq are filtered, default: %(default)d")

    parser.add_argument('--max_depth', type=int, default=None,
                        help="EXPERIMENTAL: Maximum full alignment depth to be processed. default: %(default)s")

    parser.add_argument('--indel_min_af', type=float, default=0.2,
                        help="EXPERIMENTAL: Minimum indel allele frequency for a site to be considered as a candidate site, default: %(default)f")

    # options for debug purpose
    parser.add_argument('--phasing_info_in_bam', action='store_true',
                        help="DEBUG: Skip phasing and use the phasing info provided in the input BAM (HP tag), default: False")

    parser.add_argument('--phasing_window_size', type=int, default=param.phasing_window_size,
                        help="DEBUG: The window size for read phasing")

    parser.add_argument('--extend_bed', nargs='?', action="store", type=str, default=None,
                        help="DEBUG: Extend the regions in the --bed_fn by a few bp for tensor creation, default extend 16bp")

    parser.add_argument('--alt_fn', type=str, default=None,
                        help="DEBUG: Output all alternative indel cigar for debug purpose")

    parser.add_argument('--truth_vcf_fn', type=str, default=None,
                        help="Candidate sites VCF file input, if provided, variants will only be called at the sites in the VCF file,  default: %(default)s")

    # options for internal process control
    ## Path to the 'zstd' compression
    parser.add_argument('--zstd', type=str, default=param.zstd,
                        help=SUPPRESS)

    ## Test in specific candidate position. Only for testing
    parser.add_argument('--test_pos', type=str2bool, default=0,
                        help=SUPPRESS)

    ## The number of chucks to be divided into for parallel processing
    parser.add_argument('--chunk_num', type=int, default=None,
                        help=SUPPRESS)

    ## The chuck ID to work on
    parser.add_argument('--chunk_id', type=int, default=None,
                        help=SUPPRESS)

    ## Provide the regions to be included in full-alignment based calling
    parser.add_argument('--candidates_bed_regions', type=str, default=None,
                        help=SUPPRESS)

    ## Use Clair3's own phasing module for read level phasing when creating tensor, compared to using Whatshap, speed is faster but has higher memory footprint, default: False
    parser.add_argument('--need_phasing', action='store_true',
                        help=SUPPRESS)

    parser.add_argument('--tensor_sample_mode', type=str2bool, default=0,
                        help=SUPPRESS)

    parser.add_argument('--phase_tumor', type=str2bool, default=0,
                        help=SUPPRESS)

    parser.add_argument('--training_mode', type=str2bool, default=0,
                        help=SUPPRESS)

    parser.add_argument('--proportion', type=float, default=1.0,
                        help=SUPPRESS)


    args = parser.parse_args()

    create_tensor(args)


if __name__ == "__main__":
    main()

