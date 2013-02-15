#!/usr/bin/env python
from optparse import OptionParser
import copy, gzip, os, subprocess, sys
import pysam

################################################################################
# tophat_growing.py
#
# Align reads with tophat where we initially start with a 5' seed and grow
# outward from there only if the read multimaps.
#
# Assuming all reads are the same length.
################################################################################


################################################################################
# main
################################################################################
def main():
    usage = 'usage: %prog [options] <bowtie index> <reads1> [<reads2> ...]'
    parser = OptionParser(usage)

    # shrinking options
    parser.add_option('-s', '--initial_seed', dest='initial_seed', type='int', default=18, help='Seed length to initially consider for aligning the reads [Default: %default]')

    # tophat options
    parser.add_option('-p', '--num_threads', dest='num_threads', type='int', default=2, help='# of TopHat threads to launch [Default: %default]')
    parser.add_option('-G','--GTF', dest='gtf_file', help='Reference GTF file')
    parser.add_option('--transcriptome-index', dest='tx_index', default='txome', help='Transcriptome bowtie2 index [Default: %default]')
    #parser.add_option('--no-novel-juncs', dest='no_novel_juncs', type='bool', action='store_true', help='Do not search for novel splice junctions [Default: %default]')
    (options,args) = parser.parse_args()

    # parse required input
    if len(args) < 2:
        parser.error(usage)
    else:
        bowtie_index = args[0]
        fastq_files = args[1:]

    # find read length
    full_read_length = fastq_read_length(fastq_files[0])
    print >> sys.stderr, 'Read length: %d' % full_read_length

    # make a tmp dir for sorting
    os.mkdir('tmp_sort')

    read_len = options.initial_seed
    while read_len <= full_read_length:
        # prepare fastq file
        if read_len == options.initial_seed:
            # trim reads
            initial_fastq(fastq_files, read_len)
            fastq_nonempty = True
        else:
            # update fastq for multimappers and grow
            fastq_nonempty = update_fastq(fastq_files, read_len)

        if not fastq_nonempty:
            break

        # align
        subprocess.call('tophat -o thout%d -p %d -G %s --no-novel-juncs --transcriptome-index=%s %s iter.fq' % (read_len, options.num_threads, options.gtf_file, options.tx_index, bowtie_index), shell=True)

        # parse BAM to split unique and multimapped
        split_iter_bam(read_len)

        # split lost multimappers from previous iteration
        if read_len > options.initial_seed:
            split_lost_multi(read_len-1)

        # for debug purposes for now
        #os.rename('iter.fq', 'thout%d/iter.fq' % read_len)

        read_len += 1

    # save remaining multimappers
    split_lost_multi(read_len-1, write_all=True)

    # clean up
    os.remove('iter.fq')
    os.rmdir('tmp_sort')

    # combine all alignments
    bam_files = []
    for rl in range(options.initial_seed, read_len):
        bam_files.append('thout%d/unique.bam' % rl)
        bam_files.append('thout%d/lost_multi.bam' % rl)
    subprocess.call('samtools merge all.bam %s' % ' '.join(bam_files), shell=True)


################################################################################
# fastq_read_length
#
# Input
#  fastq_file: Fastq file name
#
# Output
#  read length
################################################################################
def fastq_read_length(fastq_file):
    if fastq_file[-2:] == 'gz':
        fastq_open = gzip.open(fastq_file)
    else:
        fastq_open = open(fastq_file)
    header = fastq_open.readline()
    seq = fastq_open.readline().rstrip()
    return len(seq)


################################################################################
# initial_fastq
#
# Input
#  fastq_files: List of fastq file names
#  read_len:    Length to trim the reads to
#
# Output
#  iter.fq:     New fastq file containing trimmed reads.
################################################################################
def initial_fastq(fastq_files, read_len):
    out_fq = open('iter.fq', 'w')

    for fq_file in fastq_files:
        if fq_file[-2:] == 'gz':
            fq_open = gzip.open(fq_file)
        else:
            fq_open = open(fq_file)

        header = fq_open.readline()
        while header:
            seq = fq_open.readline()
            mid = fq_open.readline()
            qual = fq_open.readline()

            print >> out_fq, header.rstrip()
            print >> out_fq, seq[:read_len].rstrip()
            print >> out_fq, '+'
            print >> out_fq, qual[:read_len].rstrip()

            header = fq_open.readline()
        fq_open.close()

    out_fq.close()


################################################################################
# split_iter_bam
#
# Input
#  read_len: Trimmed read length used to find filenames
#
# Output
#  unique.bam:   BAM file filtered for uniquely mapping reads
#  multimap.bam: BAM file filtered for multimapping reads
################################################################################
def split_iter_bam(read_len):
    # original BAM for header
    original_bam = pysam.Samfile('thout%d/accepted_hits.bam' % read_len, 'rb')

    # initialize split BAM files
    unique_bam = pysam.Samfile('thout%d/unique.bam' % read_len, 'wb', template=original_bam)
    multimap_bam = pysam.Samfile('thout%d/multimap.bam' % read_len, 'wb', template=original_bam)

    for aligned_read in original_bam:
        if aligned_read.opt('NH') == 1:
            unique_bam.write(aligned_read)
        else:
            multimap_bam.write(aligned_read)

    unique_bam.close()
    multimap_bam.close()


################################################################################
# split_lost_multi
#
# Input
#  read_len:       Trimmed read length used to find filenames. We're working in
#                   the dir defined by read_len, by will use unmapped reads
#                   from read_len+1
#  write_all:      Write all multi-mappers regardless of unmapped_set
#
# Output
#  lost_multi.bam: BAM file filtered for multimapping reads lost in the next iter
################################################################################
def split_lost_multi(read_len, write_all=False):
    # store unmapped headers    
    unmapped_set = set()
    if write_all == False and os.path.isfile('thout%d/unmapped.bam' % (read_len+1)):
        for aligned_read in pysam.Samfile('thout%d/unmapped.bam' % (read_len+1), 'rb'):
            unmapped_set.add(aligned_read.qname)

    # open multimapping bam
    multi_bam = pysam.Samfile('thout%d/multimap.bam' % read_len, 'rb')

    # initialize lost multi mapped read BAM file
    lost_multi_bam = pysam.Samfile('thout%d/lost_multi.bam' % read_len, 'wb', template=multi_bam)

    # print lost multis
    for aligned_read in multi_bam:
        if write_all or aligned_read.qname in unmapped_set:
            lost_multi_bam.write(aligned_read)

    lost_multi_bam.close()


################################################################################
# update_fastq
#
# Input
#  fastq_files:    List of fastq file names
#  read_len:       Length to trim the reads to (and find prior multimaps)
#
# Output
#  iter.fq:        New fastq file containing the trimmed reads we want
#  fastq_nonempty: True if the fastq file still contains reads.
################################################################################
def update_fastq(fastq_files, read_len):
    # store multi-mapping headers
    subprocess.call('samtools view thout%d/multimap.bam | cut -f1 | sort -u -T tmp_sort | awk \'{print "@"$0}\' > multimap.txt' % (read_len-1), shell=True)

    if os.path.getsize('multimap.txt') == 0:
        fastq_nonempty = False
    else:
        fastq_nonempty = True

        out_fq = open('iter.fq', 'w')

        for fq_file in fastq_files:
            # unzip maybe
            multi_filter_cmd = 'cat %s' % fq_file
            if fq_file[-2:] == 'gz':
                multi_filter_cmd = 'gunzip -c %s' % fq_file

            # grep for multimaps
            multi_filter_cmd += ' | grep -A3 -F -w -f multimap.txt | grep -v "^--$"'

            # filter and trim reads
            p = subprocess.Popen(multi_filter_cmd, stdout=subprocess.PIPE, shell=True)
            header = p.stdout.readline()
            while header:
                seq = p.stdout.readline()
                mid = p.stdout.readline()
                qual = p.stdout.readline()

                print >> out_fq, header.rstrip()
                print >> out_fq, seq[:read_len].rstrip()
                print >> out_fq, mid.rstrip()
                print >> out_fq, qual[:read_len].rstrip()

                header = p.stdout.readline()
            p.communicate()

        out_fq.close()

    os.remove('multimap.txt')

    return fastq_nonempty


################################################################################
# __main__
################################################################################
if __name__ == '__main__':
    main()
