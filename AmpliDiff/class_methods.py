from audioop import reverse
from heapq import heapify, heappop, heappush
from operator import truediv
from random import randrange
from classless_methods import calculate_degeneracy, equivalent_characters
import amplicon_generation
from Sequence import *
from Amplicon import *
#BIOCONDA
from Bio import SeqIO
#GUROBI
import gurobipy as gp
from gurobipy import GRB
#Standard library
import csv
import itertools

def generate_sequences(sequence_file, metadata_file, min_characters=-1, max_degeneracy=-1, max_n=-1):
    '''
    Function that reads (aligned) sequences from a fasta file and saves them 
    as Sequence objects with lineages obtained from "metadata.tsv". Note that
    this function will try to find a column with "lineage" in the header which
    will be used to assign lineages.

    Parameters
    ----------
    sequence_file : str
        Path to sequence file.
    metadata_file : str
        Path to metadata file.
    min_characters : int, optional
        Minimum length of sequences. The default is -1 in which case this is not checked.
    max_degeneracy : int, optional
        Maximum degeneracy of sequences. The default is -1 in which case this is not checked.
    max_n : int, optional
        Maximum number of 'n' nucleotides. The default is -1 in which case this is not checked

    Returns
    -------
    sequences : list
        List of sequences.

    '''
    sequences_temp = {} #temporarily stores sequences as identifier -> sequence
    to_delete = [] #contains sequences that will be deleted based on QC parameters

    #Read aligned sequences
    sequences_object = SeqIO.parse(open(sequence_file), 'fasta')
    for sequence in sequences_object:
        delete_this_seq = False #set to True when a sequence should be deleted
        sequences_temp[sequence.id.split('|')[0]] = str(sequence.seq.lower()) #stores sequences as lowercase
        if min_characters >= 0: #check if sequence is long enough
            if len(sequence.seq.replace('-','')) < min_characters:
                to_delete.append(sequence.id.split('|')[0])
                delete_this_seq = True
        elif max_degeneracy >= 0 and not delete_this_seq: #check if sequence is too degenerate
            if calculate_degeneracy(sequence.seq.lower().replace('-')) > max_degeneracy:
                to_delete.append(sequence.id.split('|')[0])
                delete_this_seq = True
        elif max_n >= 0 and not delete_this_seq: #check if sequence has too many 'n's
            if sequence.seq.lower().count('n') > max_n:
                to_delete.append(sequence.id.split('|')[0])
                delete_this_seq = True
    #Read metadata. If impossible, assign every sequence a unique lineage
    skip = -1 #used to determine header containing lineage information
    try:
        sequences = []
        for meta in csv.reader(open(metadata_file), delimiter = '\t'):
            if skip == -1: #first line is the header
                for cur_meta in range(len(meta)):
                    if 'lineage' in meta[cur_meta].lower():
                        skip = cur_meta #find line containing lineage information
                        break
            else: #line is not a header
                if meta[0] not in to_delete: #first column should contain sequence ids
                    sequences.append(Sequence(sequences_temp[meta[0].replace(' ', '')], meta[0], lineage=meta[skip]))
    except:
        print('Unable to read metadata from file either due to non-existing file or incorrect sequence ids, will assign unique lineages')
        sequences = []
        i = 0
        for identifier in sequences_temp:
            sequences.append(Sequence(sequences_temp[identifier], identifier, lineage=str(i)))
            i += 1

    return sequences

def process_sequences(sequences, min_non_align=0, amplicon_width=0, max_misalign=-1):
    '''
    Function that preprocesses the multiple aligned sequences by calculating lower- and upperbounds such that every
    sequence contains at least &min_non_align nucleotides before and after $lb and $ub$ respectively. Additionally
    also determines feasible amplicons given an amplicon width and misalignment character threshold, and finds nucleotides
    that should be considered when differentiating sequences.

    Parameters
    ----------
    sequences : list
        List with multiple aligned sequences to filter.
    min_non_align : int, optional
        Number of nucleotides to include before (after) $lb ($ub). The default is 0.
    amplicon_width : int, optional
        Size of the amplicons, if you want to determine their feasibility a priori. The default is 0 in which case feasibility of amplicons is not checked.
    max_misalign : int, optional
        Number of allowed misalign characters in an amplicon. The default is -1 in which case feasibility of amplicons is not checked.

    Returns
    -------
    sequences : list[Sequence]
        List of sequences that now include MSA to original sequence mapping.
    lb : int
        Lowerbound such that every sequence has at least $min_non_align nucleotides before it.
    ub : int
        Upperbound such that every sequence has at least $min_non_align nucleotides after it.
    feasible_amplicons : set
        Set of feasible amplicons [start, end) in case amplicon feasibility is checked.
    relevant_nucleotides : np.array
        Numpy array with the indices of nucleotides that are potentially different between pairs of sequences

    '''
    #Assert that every sequence has the same length
    for sequence in sequences[1:]:
        if sequence.length != sequences[0].length:
            raise ValueError("Sequences have varying lengths, only multiple aligned sequences can be preprocessed!")


    def find_feasible_amplicons(sequence, lb, ub, amplicon_width, max_misalign):
        '''
        Function that determines feasible amplicons based on the number of misalignment characters.

        Parameters
        ----------
        sequence : Sequence
            Sequence object for which the amplicons should be checked.
        lb : int
            Start index (inclusive) of the first amplicon.
        ub : int
            End index (exclusive) of the final amplicon.
        amplicon_width : int
            Width of the amplicons to check.
        max_misalign : int
            Maximum allowed number of misalignment characters.

        Returns
        -------
        feasible_amplicons : set
            Set containing the amplicons (start,end) which do not contain too many misalignment characters.

        '''
        feasible_amplicons = set()
        misalign_indices = []

        #Determine indices of misalignment characters in initial amplicon minus the final position
        for i in range(lb, lb+amplicon_width-1):
            if sequence.sequence[i] == '-':
                misalign_indices.append(i)
        #Iterate over amplicons between lb and ub
        for i in range(lb+amplicon_width-1, ub):
            if sequence.sequence[i] == '-': # check if next character is misalign character
                misalign_indices.append(i)
            if len(misalign_indices) <= max_misalign: # check if current amplicon has too many misalign characters
                feasible_amplicons.add((i-amplicon_width+1, i+1))
            try: # check if the first index in misalign_indices should be removed
                if misalign_indices[0] == i - amplicon_width + 1:
                    misalign_indices.pop(0)
            except:
                continue
        return feasible_amplicons
    
    feasible_amplicons = set()
    lb = 0
    ub = sequences[0].length

    #Determine feasible amplicons and positions where sequences potentially differ
    sequence_index = 0 #sequence index
    options_table = [set(['a','c','g','t','-']) for _ in range(sequences[0].length)] #set with nucleotides at every position

    for sequence in sequences:
        sequence.align_to_trim()
        (cur_lb, cur_ub) = sequence.find_bounds(min_non_align)
        lb = max(lb, cur_lb) #update lowerbound
        ub = min(ub, cur_ub) #update upperbound

        #Check nucleotides:
        #   By taking intersections at every position we find a set that is either empty (i.e. nucleotide is not identical for all sequences),
        #   or contains some characters which are shared by all sequences. In the former case we know that this position can possibly be used
        #   to differentiate, but in the latter case all sequences share at least one nucleotide at the position which means that it can't be
        #   used to differentiate (note that this considers degenerate nucleotides as equal if their intersection is non-empty).
        for c in range(sequence.length):
            options_table[c] = options_table[c].intersection(equivalent_characters(sequence.sequence[c]))

        #If amplicon width and misalign_threshold are specified, find feasible amplicons
        if amplicon_width > 0 and max_misalign >= 0:
            if sequence_index == 0:
                feasible_amplicons = find_feasible_amplicons(sequence, lb, ub, amplicon_width, max_misalign)
            else:
                feasible_amplicons = feasible_amplicons.intersection(find_feasible_amplicons(sequence, lb, ub, amplicon_width, max_misalign))
        sequence_index += 1

    #Generate array with ones for positions that should be considered
    relevant_nucleotides = np.zeros((sequences[0].length), dtype=np.int32)
    for amplicon in feasible_amplicons:
        for index in range(amplicon[0], amplicon[1]):
            if len(options_table[index]) == 0:
                relevant_nucleotides[index] = 1
    relevant_nucleotides = np.where(relevant_nucleotides == 1)[0]

    return sequences, lb, ub, feasible_amplicons, relevant_nucleotides

def translate_to_numeric(sequences, amplicons, relevant_nucleotides, comparison_matrix):
    '''
    Function that returns a numeric representation of sequences and amplicons along with a numeric represenation of the
    comparison_matrix.

    Parameters
    ----------
    sequences : list
        List with multiple aligned sequences to transform into a numeric representation.
    amplicons : list
        List with amplicons (start_index, end_index).
    relevant_nucleotides : np.array
        Numpy array with the indices of nucleotides that are potentially different between pairs of sequences.
    comparison_matrix : dict [ (char,char) ]
        Dictionary that determines which characters should be considered equal.

    Returns
    -------
    chars2num : dict{ char } -> num
        Dictionary mapping characters to numbers.
    char_comp : np.array
        Numpy array (chars x chars) with a 1 if chars are unequal (i.e. have disjoint representations).
    seqs_num : np.array
        Numpy array with the numeric representation of the multiple aligned sequences.
    AMPS : np.array
        Numpy array where every entry contains 
        (starting_index, first relevant nucleotide before amplicon, first relevant nucleotide after amplicon).
    '''
    chars = ['a','c','t','g','u','r','y','k','m','s','w','b','d','h','v','n','-']
    char_comp = np.zeros((len(chars), len(chars)), dtype=np.int8)
    chars2num = {}
    seqs_num = np.zeros((len(sequences), sequences[0].length), dtype=np.int8)
    AMPS = np.zeros((len(amplicons), 3), dtype=np.int32)
    
    for char_index in range(len(chars)):
        chars2num[chars[char_index]] = char_index
        
    for c1 in range(len(chars)):
        for c2 in range(len(chars)):
            if not comparison_matrix[(chars[c1], chars[c2])][0]:
                char_comp[c1][c2] = 1
                
    for s in range(len(sequences)):
        for i in range(sequences[s].length):
            seqs_num[s][i] = chars2num[sequences[s].sequence[i]]
            
    for a in range(len(amplicons)):
        AMPS[a][0] = amplicons[a][0]
        cur = np.where(relevant_nucleotides < amplicons[a][0])[0]
        if cur.shape[0] > 0:
            AMPS[a][1] = cur[-1]
        cur = np.where(relevant_nucleotides < amplicons[a][1])[0]
        if cur.shape[0] > 0:
            AMPS[a][2] = cur[-1]
                
    return chars2num, char_comp, seqs_num, AMPS

def generate_amplicons(sequences, amplicon_width, comparison_matrix, lb=None, 
                        ub=None, max_mismatch=1, feasible_amplicons=set(), relevant_nucleotides=None):
    '''
    Function that determines which sequence pairs can be differentiated for every 
    amplicon in either $feasible_amplicons or in all possible amplicons.

    Parameters
    ----------
    sequences : list
        List of multiple aligned sequences that will be differentiated.
    amplicon_width : int
        Width of amplicons in number of nucleotides.
    comparison_matrix : dict [ (char,char) ]
        Dictionary that determines which characters should be considered equal.
    lb : int, optional
        Index from which amplicons should be generated if no feasible amplicons are supplied. The default is None in which case it is set to 0.
    ub : int, optional
        Last index (exclusive) where the final amplicon ends if they need to be generated. The default is None it is set to the length of the sequences.
    max_mismatch : int, optional
        Maximum number of allowed nucleotide mismatches between sequences in an amplicon. The default is 1.
    feasible_amplicons : set, optional
        Set of amplicons which are defined as (start_index, end_index) where the end index is exclusive. The default is set() in which case amplicons will be generated.
    relevant_nucleotides : np.array, optional
        Numpy array with indices of nucleotides that can be different among sequences. The default is None.

    Returns
    -------
    res : list[ Amplicon ]
        List of final amplicons.
    X : np.array
        Numpy array containing 3 axes:
            amplicon
            sequence
            sequence
        where X[k,i,j] = 1 iff sequence i and j can be differentiated according to amplicon k.

    '''
    #Assert that every sequence has the same length
    for sequence in sequences[1:]:
        if sequence.length != sequences[0].length:
            raise ValueError("Sequences have varying lengths, only multiple aligned sequences can be preprocessed!")
    #Assert that amplicons do not exceed sequence lengths and have equal length
    if len(feasible_amplicons) > 0:
        for amplicon in feasible_amplicons:
            if amplicon[0] < 0 or amplicon[1] > sequences[0].length:
                raise ValueError("Invalid amplicons provided, please make sure that amplicons are defined within the length of sequences")
            if amplicon[1] - amplicon[0] != amplicon_width:
                raise ValueError("Unequal amplicon lengths found, please make sure that provided amplicons have identical lengths")

    if not lb:
        lb = 0
    else:
        lb = max(lb, 0)
    if not ub:
        ub = sequences[0].length         
    else:
        ub = min(ub, sequences[0].length)

    #Check if feasible amplicons are provided
    if len(feasible_amplicons) > 0:
        amplicons = list(feasible_amplicons)
        amplicons.sort(key = lambda x : x[0]) # sort amplicons based on starting index (descending)
    else:
        amplicons = [(i, i+amplicon_width) for i in range(lb, ub-lb-amplicon_width+1)]

    #Transform input to numeric representation
    print('Transforming input sequences to numeric representations')
    lineages_list = [sequence.lineage_num for sequence in sequences]
    ids_list = np.array([sequence.id_num for sequence in sequences], dtype=np.int32)
    _, comparison_matrix_num, sequences_num, AMPS = translate_to_numeric(sequences, amplicons, relevant_nucleotides, comparison_matrix)
    print('Done transforming input sequences')

    #Determine pairs of sequences with different lineages
    print('Determining sequence pairs with different lineages')
    sequence_pairs = []
    for seq_1 in range(len(sequences)):
        for seq_2 in range(seq_1):
            if sequences[seq_1].lineage != sequences[seq_2].lineage:
                sequence_pairs.append([seq_2, seq_1])
    sequence_pairs = np.array(sequence_pairs, dtype=np.int32)
    print('Done determining pairs')

    print('Calculating amplicon differentiabilities')
    X = amplicon_generation.generate_amplicons_cy(AMPS, amplicon_width, AMPS.shape[0], sequences_num,
                                                    sequence_pairs, len(sequence_pairs), sequences_num.shape[0],
                                                    ids_list, comparison_matrix_num, relevant_nucleotides,
                                                    relevant_nucleotides.shape[0], max_mismatch)
    X = np.asarray(X, dtype=np.int8)
    print('Done calculating differentiabilities')

    res = []
    for amplicon_index in range(AMPS.shape[0]):
        res.append(Amplicon(AMPS[amplicon_index][0], AMPS[amplicon_index][0]+amplicon_width))

    return res, X
    
def check_primer_feasibility_single_amplicon_full_coverage(sequences, amplicon, differences, primer_index, temperature_range=5, feasibility_check=False):
    '''
    Function that solves the primer feasibility problem in the case of 100% required coverage.

    Parameters
    ----------
    sequences : list
        List of multiple aligned sequences.
    amplicon : Amplicon
        Amplicon object for which amplifiability will be checked.
    differences : np.array
        2-D matrix (sequences by sequences) with a 1 if the pair can be differentiated by this amplicon.
    primer_index : PrimerIndex
        PrimerIndex object containing all the primers.
    temperature_range : float, optional
        Maximum difference between minimal primer melting temperature and maximal primer melting temperature. Default is 5.
    feasibility_check : bool, optional
        If true, will only search for feasibility of the problem instance, otherwise will find minimal primer sets. Default is False.

    Returns
    -------
    list
        List of the form [bool, dict{'forward':[],'reverse':[]}, differences, list] where:
            -bool indicates whether problem instance is feasible
            -dict contains the selected forward and reverse primers (only when feasibility_check is False)
            -differences is the differences matrix given as input
            -list with sequences for which the amplicon has binding forward AND reverse primers

    '''

    if not feasibility_check:
        return check_primer_feasibility_single_amplicon_full_coverage_heuristic(sequences, amplicon, differences, primer_index, temperature_range)

    #Start environment and disable output logging
    env = gp.Env(empty=True)
    env.setParam('OutputFlag',0)
    env.start()

    #Make model and set objective to minimization
    model = gp.Model(env=env)
    model.ModelSense = GRB.MINIMIZE

    #Primer variables
    forward_primers = {} #primer -> (variable, temperature)
    reverse_primers = {} #primer -> (variable, temperature)
    #Sequence variables
    covered_binary = {} #sequence_id -> variable

    #Initialize primer and sequence variables
    for sequence in amplicon.primers['forward']:
        for primer in amplicon.primers['forward'][sequence]:
            if primer not in forward_primers:
                forward_primers[primer] = (model.addVar(vtype=GRB.BINARY, obj=0), primer_index.index2primer['forward'][primer].temperature)
    for sequence in amplicon.primers['reverse']:
        for primer in amplicon.primers['reverse'][sequence]:
            if primer not in reverse_primers:
                reverse_primers[primer] = (model.addVar(vtype=GRB.BINARY, obj=0), primer_index.index2primer['reverse'][primer].temperature)
    for sequence in sequences:
        covered_binary[sequence.id_num] = model.addVar(vtype=GRB.BINARY, obj=0)
    
    #If feasibility_check is True, model will not optimize, but only focus on finding a feasible solution
    if feasibility_check:
        num_primer_pairs = model.addVar(vtype=GRB.INTEGER, obj=0)
    else:
        num_primer_pairs = model.addVar(vtype=GRB.INTEGER, obj=1)

    #Temperature variables
    max_temp = model.addVar(vtype=GRB.CONTINUOUS, obj=0)
    min_temp = model.addVar(vtype=GRB.CONTINUOUS, obj=0)

    #Enforce covered_binary variables to 0 if they aren't covered
    for sequence in sequences:
        model.addConstr(covered_binary[sequence.id_num] <= sum(forward_primers[primer][0] for primer in amplicon.primers['forward'][sequence.id_num]))
        model.addConstr(covered_binary[sequence.id_num] <= sum(reverse_primers[primer][0] for primer in amplicon.primers['reverse'][sequence.id_num]))
        #Every sequence should be covered
        model.addConstr(covered_binary[sequence.id_num] >= 1)
        #Temperature constraints
        for primer in amplicon.full_primerset['forward']: #iterate over forward primers
            model.addConstr( min_temp <= primer_index.index2primer['forward'][primer].temperature * (3 - 2 * forward_primers[primer][0]) )
            model.addConstr( max_temp >= primer_index.index2primer['forward'][primer].temperature * forward_primers[primer][0] )
        for primer in amplicon.full_primerset['reverse']:
            model.addConstr( min_temp <= primer_index.index2primer['reverse'][primer].temperature * (3 - 2 * reverse_primers[primer][0]) )
            model.addConstr( max_temp >= primer_index.index2primer['reverse'][primer].temperature * reverse_primers[primer][0] )
    model.addConstr(max_temp - min_temp <= temperature_range)

    #Check primer conflicts
    for pair in itertools.combinations(forward_primers.keys(), 2):
        model.addConstr( forward_primers[pair[0]][0] + forward_primers[pair[1]][0] <= primer_index.check_conflict( [primer_index.index2primer['forward'][pair[0]], primer_index.index2primer['forward'][pair[1]]] ) )
    for pair in itertools.combinations(reverse_primers.keys(), 2):
        model.addConstr( reverse_primers[pair[0]][0] + reverse_primers[pair[1]][0] <= primer_index.check_conflict( [primer_index.index2primer['reverse'][pair[0]], primer_index.index2primer['reverse'][pair[1]]] ) )
    for fwd in forward_primers:
        for rev in reverse_primers:
            model.addConstr( forward_primers[fwd][0] + reverse_primers[rev][0] <= primer_index.check_conflict( [primer_index.index2primer['forward'][fwd], primer_index.index2primer['reverse'][rev]] ) )

    #Set variable for number of primer pairs
    model.addConstr(num_primer_pairs >= sum(forward_primers[primer][0] for primer in forward_primers))
    model.addConstr(num_primer_pairs >= sum(reverse_primers[primer][0] for primer in reverse_primers))

    #Optimize model and if solution is optimal return outcome, otherwise report that no feasible solution exists
    model.optimize()
    if model.Status == 2:
        res = {'forward' : [], 'reverse' : []}
        seqs_covered = 0
        print('Forward primers: ')
        for primer in forward_primers:
            if forward_primers[primer][0].x > 0.9:
                print(primer_index.index2primer['forward'][primer].sequence)
                res['forward'].append(primer_index.index2primer['forward'][primer].sequence)
        print('Reverse primers: ')
        for primer in reverse_primers:
            if reverse_primers[primer][0].x > 0.9:
                print(primer_index.index2primer['reverse'][primer].sequence)
                res['reverse'].append(primer_index.index2primer['reverse'][primer].sequence)
        for sequence in covered_binary:
            if covered_binary[sequence].x > 0.9:
                seqs_covered += 1/len(sequences)
        return [True, res, differences, seqs_covered]
    return [False, None, None, None]

num_perturbances = 5
num_local_search_iterations = 20
X_perturbance = 0.75
X_local_search = 0.25

def check_primer_feasibility_single_amplicon_full_coverage_heuristic(sequences, amplicon, differences, primer_index, temperature_range=5):
        # for plotting results
        best_solution_size = []
        found_solution_size = []
        did_find_feasible_solution = []
        
        # initialise sequences and whether they are covered
        covered_sequences = {} # dictionary for sequence coverage: (seq ID -> (Bool, Bool)) where Bool determines whether there is a primer for that direction (either forward or reverse)
        sequence_dict = {} # dictionary for sequence IDs to sequence objects: (seq ID -> seq)
        for sequence in sequences:
            # CHANGE THIS TO ID_NUM
            covered_sequences[sequence.id] = (False, False)
            sequence_dict[sequence.id] = sequence

        # initialise primer sets
        # the primer lists contain tuples of (differentiability, primer)
        all_forward_primers = []
        all_reverse_primers = []
        for orientation in amplicon.primers:
            if orientation == 'forward':
                primer_list = all_forward_primers
            else:
                primer_list = all_reverse_primers
            for sequence in amplicon.primers[orientation]:
                for primer in amplicon.primers[orientation][sequence]:
                    cur_primer = primer_index.index2primer[orientation][primer]
                    if cur_primer not in map(lambda x: x[1], primer_list) and cur_primer.feasible:
                        differentiability = 0
                        for index in cur_primer.indices:
                            print("it's the index!")
                            print(index)
                            # doesnt work at the moment...
                            if index in covered_sequences:
                                # never gets here...
                                differentiability += 1
                        if differentiability > 0:
                            heappush(primer_list, (differentiability, cur_primer))
        
        # solution contains only primers, not (differentiability, primer) tuples
        currently_used_primers = []
        primer_penalties = {} # dict from (primer seq -> primer[]) where primer[] contains all primers that the primer has had conflicts with
        temp_penalties = {} # dict from (primer seq -> primer[]) where primer[] contains all primers that could not be added due to the temperature of primer
        new_solution, all_forward_primers, all_reverse_primers, covered_sequences, min_temp_primer, max_temp_primer = do_search_cycle(currently_used_primers, all_forward_primers, all_reverse_primers, covered_sequences, amplicon, primer_index, sequences, temperature_range, None, None, primer_penalties, temp_penalties)
        best_solution = new_solution

        for pert in range(num_perturbances):
            new_starting_point, covered_sequences, all_forward_primers, all_reverse_primers, min_temp_primer, max_temp_primer = remove_X_primers(best_solution, X_perturbance, amplicon, primer_index, sequences)
            new_solution, all_forward_primers, all_reverse_primers, covered_sequences, min_temp_primer, max_temp_primer = do_search_cycle(new_starting_point, all_forward_primers, all_reverse_primers, covered_sequences, amplicon, primer_index, sequences, temperature_range, min_temp_primer, max_temp_primer, primer_penalties, temp_penalties)
            if new_solution is not None:
                if best_solution is None or (len(new_solution) < len(best_solution)):
                    best_solution = new_solution
        
        print("Final solution size!!!")
        print(len(best_solution))

        # final primer set for output
        final_solution = {'forward': [], 'reverse': []}
        for primer in best_solution:
            if primer.orientation == 'forward':
                final_solution['forward'].append(primer.sequence)
            else:
                final_solution['reverse'].append(primer.sequence)

        # covered sequences for output
        properly_covered_sequences = []
        for seq, covered in covered_sequences.items():
            if covered[0] and covered[1]:
                properly_covered_sequences.append(seq)
        map(lambda x: sequence_dict[x], properly_covered_sequences)
        
        # assumption is made that solution is always feasible
        return [True, final_solution, differences, properly_covered_sequences]

def do_search_cycle(currently_used_primers, all_forward_primers, all_reverse_primers, covered_sequences, amplicon, primer_index, sequences, temperature_range, min_temp_primer, max_temp_primer, primer_penalties, temp_penalties):
    # initially there is no best solution
    best_solution = None
    solution = currently_used_primers
    
    for loc_search in range(num_local_search_iterations):
        has_feasible_solution = True
        is_forward_covered, is_reversed_covered = is_full_coverage(covered_sequences)
        while(not is_forward_covered and not is_reversed_covered):
            if not is_forward_covered:
                cur_forward_primer, min_temp_primer, max_temp_primer = find_compatible_primer(all_forward_primers, solution, primer_index, temperature_range, min_temp_primer, max_temp_primer, primer_penalties, temp_penalties)
                if cur_forward_primer is None:
                    has_feasible_solution = False
                    break
            if not is_reversed_covered:
                cur_reverse_primer, min_temp_primer, max_temp_primer = find_compatible_primer(all_reverse_primers, solution, primer_index, temperature_range, min_temp_primer, max_temp_primer, primer_penalties, temp_penalties)
                if cur_reverse_primer is None:
                    has_feasible_solution is False
                    break

            # in case all forward primers are already covered
            if is_forward_covered:
                heappop(all_reverse_primers)
                solution.append(cur_reverse_primer[1])
                for index in cur_reverse_primer[1].indices:
                    # check whether this sequence was part of the input sequences
                    if index in covered_sequences:
                        covered_sequences[index][1] = True
            elif is_reversed_covered:
                heappop(all_forward_primers)
                solution.append(cur_forward_primer[1])
                for index in cur_forward_primer[1].indices:
                    if index in covered_sequences:
                        covered_sequences[index][0] = True
            elif cur_forward_primer[0] > cur_reverse_primer[0]:
                heappop(all_forward_primers)
                # only add the primer, not how many sequences it covers
                solution.append(cur_forward_primer[1])
                for index in cur_forward_primer[1].indices:
                    if index in covered_sequences:
                        covered_sequences[index][0] = True # get sequence ID from sequence indices from primer and set those to True
            else:
                heappop(all_reverse_primers)
                solution.append(cur_reverse_primer[1])
                for index in cur_reverse_primer[1].indices:
                    if index in covered_sequences:
                        covered_sequences[index][1] = True
            is_forward_covered, is_reversed_covered = is_full_coverage(covered_sequences)
            # sort at end, as first loop will always be sorted anyways through either remove_X or the initial heaps creation
            all_forward_primers, all_reverse_primers = sort_primers_on_differentiability(all_forward_primers, all_reverse_primers, covered_sequences)
        if has_feasible_solution and ((best_solution is None) or (len(solution) < len(best_solution))):
                best_solution = solution
        if has_feasible_solution:
            print("has feasible solution")
        else:
            print("NO feasible solution")
        # if there is no feasible solution, simply look at best solution again
        if best_solution is None:
            # if there is no best solution, reset to start of this local search iteration and try again with updated penalty dictionaries
            print("first solution was infeasible")
            solution, covered_sequences, all_forward_primers, all_reverse_primers, min_temp_primer, max_temp_primer = remove_X_primers(currently_used_primers, 0, amplicon, primer_index, sequences)
        else:
            solution, covered_sequences, all_forward_primers, all_reverse_primers, min_temp_primer, max_temp_primer = remove_X_primers(best_solution, X_local_search, amplicon, primer_index, sequences)
    return best_solution, all_forward_primers, all_reverse_primers, covered_sequences, min_temp_primer, max_temp_primer

# determines whether all sequences are covered in the 2 directions
def is_full_coverage(sequences):
    is_forward_covered = True
    is_reversed_covered = True
    for seq, covered in sequences.items():
        if not covered[0]:
            is_forward_covered = False
            if not is_forward_covered and not is_reversed_covered:
                break
        if not covered[1]:
            is_reversed_covered = False
            if not is_forward_covered and not is_reversed_covered:
                break
    return (is_forward_covered, is_reversed_covered)

# finds and returns compatible primer from primer_list
def find_compatible_primer(primer_list, solution, primer_index, temperature_range, min_temp_primer, max_temp_primer, primer_penalties, temp_penalties):
    cur_primer = None
    is_primer_compatible = False
    while(not is_primer_compatible):
            # in case there are no more possible primers
            if len(primer_list) == 0:
                return None, -1, -1
            cur_primer = primer_list[0] # peek at top of heap
            is_primer_compatible = True
            # check for primer primer compatibility
            for primer in solution:
                # assuming that a check_conflict is always assigned and never yields -1
                if primer_index.check_conflict((cur_primer[1], primer)) == 1:
                    conflict_cur_primer = primer_penalties.get(cur_primer[1].sequence, [])
                    if primer not in conflict_cur_primer:
                        conflict_cur_primer.append(primer)
                    conflict_other_primer = primer_penalties.get(primer.sequence, [])
                    if cur_primer[1] not in conflict_other_primer:
                        conflict_other_primer.append(cur_primer[1])
                    is_primer_compatible = False
                    break
            # chance of adding primers lessens based on conflicts it has had with other primers
            if randrange(0, len(primer_penalties.get(cur_primer[1].sequence, [])) + 1) != 0:
                print("removed primer due to penalty")
                is_primer_compatible = False
                    
            if is_primer_compatible:
                # do temperature check
                cur_primer_temp = cur_primer[1].temperature
                # i.e. this is the first primer we add
                if max_temp_primer is None:
                    max_temp_primer = cur_primer[1]
                    min_temp_primer = cur_primer[1]
                if (cur_primer_temp - min_temp_primer.temperature <= temperature_range) and (max_temp_primer.temperature - cur_primer_temp <= temperature_range):
                    if cur_primer_temp < min_temp_primer.temperature:
                        min_temp_primer = cur_primer[1]
                    if cur_primer_temp > max_temp_primer.temperature:
                        max_temp_primer = cur_primer[1]
                else:
                    # primer cannot be added because its temp is too high
                    if not (cur_primer_temp - min_temp_primer.temperature <= temperature_range):
                        min_temp_primer_penalty = temp_penalties.get(min_temp_primer.sequence, [])
                        if cur_primer[1] not in min_temp_primer_penalty:
                            min_temp_primer_penalty.append(cur_primer[1])
                    # primer cannot be added because its temp is too low
                    elif not (max_temp_primer.temperature - cur_primer_temp <= temperature_range):
                        max_temp_primer_penalty = temp_penalties.get(max_temp_primer.sequence, [])
                        if cur_primer[1] not in max_temp_primer_penalty:
                            max_temp_primer_penalty.append(cur_primer[1])
                    is_primer_compatible = False
                # chance of adding primer lessens based on number of other primers its temperature clashes with
                if randrange(0, len(temp_penalties.get(cur_primer[1].sequence, [])) + 1) != 0:
                    print("removed primer due to penalty")
                    is_primer_compatible = False
            if not is_primer_compatible:
                heappop(primer_list)
    return cur_primer, min_temp_primer, max_temp_primer

# updates differentiability of the primers, returns updated lists
def sort_primers_on_differentiability(all_forward_primers, all_reverse_primers, covered_sequences):
    updated_forward_primers = []
    while(len(all_forward_primers) != 0):
        cur_primer = heappop(all_forward_primers)
        already_in_there = 0
        not_part_of_input = 0
        for seq in cur_primer[1].indices:
            if seq in covered_sequences:
                if covered_sequences[seq][0]:
                    already_in_there += 1
            else:
                not_part_of_input += 1
        updated_forward_primers.append((len(cur_primer[1].indices) - already_in_there - not_part_of_input, cur_primer[1]))
    heapify(updated_forward_primers)
    
    updated_reverse_primers = []
    while(len(all_reverse_primers) != 0):
        cur_primer = heappop(all_reverse_primers)
        already_in_there = 0
        not_part_of_input = 0
        for seq in cur_primer[1].indices:
            if seq in covered_sequences:
                if covered_sequences[seq][1]:
                    already_in_there += 1
            else:
                not_part_of_input += 1
        updated_reverse_primers.append((len(cur_primer[1].indices) - already_in_there - not_part_of_input, cur_primer[1]))
    heapify(updated_reverse_primers)
    
    return updated_forward_primers, updated_reverse_primers

# removes a percentage of primers from the given solution, then recreates heaps and covered_sequences
def remove_X_primers(solution, percentage_to_remove, amplicon, primer_index, sequences):
    amount_to_remove = int(len(solution) * percentage_to_remove)
    np.random.shuffle(solution)
    print(amount_to_remove)
    new_partial_solution = solution[amount_to_remove:] # this takes a sublist from the shuffled original
    if len(new_partial_solution) != 0:
        min_temp_primer = new_partial_solution[0]
        max_temp_primer = min_temp_primer
    else:
        min_temp_primer = None
        max_temp_primer = min_temp_primer
    
    new_all_forward_primers = []
    new_all_reverse_primers = []
    for orientation in amplicon.primers:
        if orientation == 'forward':
            primer_list = new_all_forward_primers
        else:
            primer_list = new_all_reverse_primers
        for sequence in amplicon.primers[orientation]:
            for primer in amplicon.primers[orientation][sequence]:
                cur_primer = primer_index.index2primer[orientation][primer]
                if cur_primer not in map(lambda x: x[1], primer_list) and cur_primer not in new_partial_solution and cur_primer.feasible:
                    primer_list.append((-1, cur_primer))
                    #heappush(primer_list, (0, cur_primer))
    
    new_covered_sequences = {}
    for sequence in sequences:
        new_covered_sequences[sequence.id] = (False, False)

    for primer in new_partial_solution:
        if primer.temperature < min_temp_primer.temperature:
            min_temp_primer = primer
        if primer.temperature > max_temp_primer.temperature:
            max_temp_primer = primer
        for index in primer.indices:
            if index in new_covered_sequences:
                if primer.orientation == 'forward':
                    new_covered_sequences[index][0] = True
                else:
                    new_covered_sequences[index][1] = True
    # to get correct amplifiability for primer tuples on the heaps
    new_all_forward_primers, new_all_reverse_primers = sort_primers_on_differentiability(new_all_forward_primers, new_all_reverse_primers, new_covered_sequences)
    return new_partial_solution, new_covered_sequences, new_all_forward_primers, new_all_reverse_primers, min_temp_primer, max_temp_primer



def check_primer_feasibility_single_amplicon_variable_coverage(sequences, amplicon, differences, total_differences, 
                                                                primer_index, temperature_range=5, beta=0.05, coverage=1):
    '''
    Function that solves the primer feasibility problem in the case of <100% required coverage.

    Parameters
    ----------
    sequences : list
        List of multiple aligned sequences.
    amplicon : Amplicon
        Amplicon object for which amplifiability will be checked.
    differences : np.array
        2-D matrix (sequences by sequences) with a 1 if the pair can be differentiated by this amplicon.
    total_differences : int
        Total differentiability of this amplicon
    primer_index : PrimerIndex
        PrimerIndex object containing all the primers.
    temperature_range : float, optional
        Maximum difference between minimal primer melting temperature and maximal primer melting temperature. Default is 5.
    beta : float, optional
        Parameter modelling the trade-off between adding primer pairs and increasing differentiability. The default is 0.05.
    coverage : float, optional
        Percentage of input sequence in which amplicon must be amplifiable. The default is 100%

    Returns
    -------
    list
        List of the form [bool, dict{'forward':[],'reverse':[]}, differences, list] where:
            -bool indicates whether problem instance is feasible
            -dict contains the selected forward and reverse primers (only when feasibility_check is False)
            -differences is an adjusted version of the input differences matrix with 1s for amplicons that are amplifiable
            -list with sequences for which the amplicon has binding forward AND reverse primers

    '''
    #Start environment and disable output logging
    env = gp.Env(empty=True)
    env.setParam('OutputFlag',0)
    env.start()

    #Make model and set objective to maximize
    model = gp.Model(env=env)
    model.ModelSense = GRB.MAXIMIZE

    #Primer variables
    forward_primers = {} #primer -> (variable, temperature)
    reverse_primers = {} #primer -> (variable, temperature)
    #Sequence variables
    covered_binary = {} #sequence_id -> variable
    covered_pairs = {} #(sequence_id, sequence_id) -> variable

    #Initialize primer and sequence variables
    for sequence in amplicon.primers['forward']:
        for primer in amplicon.primers['forward'][sequence]:
            if primer not in forward_primers:
                forward_primers[primer] = (model.addVar(vtype=GRB.BINARY, obj=0), primer_index.index2primer['forward'][primer].temperature)
    for sequence in amplicon.primers['reverse']:
        for primer in amplicon.primers['reverse'][sequence]:
            if primer not in reverse_primers:
                reverse_primers[primer] = (model.addVar(vtype=GRB.BINARY, obj=0), primer_index.index2primer['reverse'][primer].temperature)
    for sequence in sequences:
        covered_binary[sequence.id_num] = model.addVar(vtype=GRB.BINARY, obj=0)
    for s1 in range(len(sequences)):
        for s2 in range(s1):
            if sequences[s1].lineage != sequences[s2].lineage and differences[sequences[s2].id_num, sequences[s1].id_num] == 1:
                covered_pairs[(sequences[s1].id_num, sequences[s2].id_num)] = model.addVar(vtype=GRB.BINARY, obj=1)
                model.addConstr(covered_pairs[(sequences[s1].id_num, sequences[s2].id_num)] <= 0.5*covered_binary[sequences[s1].id_num] + 0.5*covered_binary[sequences[s2].id_num])
    num_primer_pairs = model.addVar(vtype=GRB.INTEGER, obj=-beta*total_differences)

    #Temperature variables
    max_temp = model.addVar(vtype=GRB.CONTINUOUS, obj=0)
    min_temp = model.addVar(vtype=GRB.CONTINUOUS, obj=0)

    #Enforce covered_binary variables to 0 if sequences aren't covered (i.e. no binding primers)
    for sequence in sequences:
        model.addConstr(covered_binary[sequence.id_num] <= sum(forward_primers[primer][0] for primer in amplicon.primers['forward'][sequence.id_num]))
        model.addConstr(covered_binary[sequence.id_num] <= sum(reverse_primers[primer][0] for primer in amplicon.primers['reverse'][sequence.id_num]))
        #At least $coverage (fraction) of the sequences should be covered per amplicon
        model.addConstr(sum(covered_binary[sequence.id_num] for sequence in sequences) >= coverage * len(sequences))
        #Temperature constraints
        for primer in amplicon.full_primerset['forward']: #iterate over forward primers
            model.addConstr( min_temp <= primer_index.index2primer['forward'][primer].temperature * (3 - 2 * forward_primers[primer][0]) )
            model.addConstr( max_temp >= primer_index.index2primer['forward'][primer].temperature * forward_primers[primer][0] )
        for primer in amplicon.full_primerset['reverse']:
            model.addConstr( min_temp <= primer_index.index2primer['reverse'][primer].temperature * (3 - 2 * reverse_primers[primer][0]) )
            model.addConstr( max_temp >= primer_index.index2primer['reverse'][primer].temperature * reverse_primers[primer][0] )
    model.addConstr(max_temp - min_temp <= temperature_range)

    #Check primer conflicts
    for pair in itertools.combinations(forward_primers.keys(), 2):
        model.addConstr( forward_primers[pair[0]][0] + forward_primers[pair[1]][0] <= primer_index.check_conflict( [primer_index.index2primer['forward'][pair[0]], primer_index.index2primer['forward'][pair[1]]] ) )
    for pair in itertools.combinations(reverse_primers.keys(), 2):
        model.addConstr( reverse_primers[pair[0]][0] + reverse_primers[pair[1]][0] <= primer_index.check_conflict( [primer_index.index2primer['reverse'][pair[0]], primer_index.index2primer['reverse'][pair[1]]] ) )
    for fwd in forward_primers:
        for rev in reverse_primers:
            model.addConstr( forward_primers[fwd][0] + reverse_primers[rev][0] <= primer_index.check_conflict( [primer_index.index2primer['forward'][fwd], primer_index.index2primer['reverse'][rev]] ) )

    #Set variable for primer pairs
    model.addConstr(num_primer_pairs >= sum(forward_primers[primer][0] for primer in forward_primers))
    model.addConstr(num_primer_pairs >= sum(reverse_primers[primer][0] for primer in reverse_primers))

    model.optimize()
    if model.Status == 2:
        res = {'forward' : [], 'reverse' : []}
        seqs_covered = 0
        print('Forward primers: ')
        for primer in forward_primers:
            if forward_primers[primer][0].x > 0.9:
                print(primer_index.index2primer['forward'][primer].sequence)
                res['forward'].append(primer_index.index2primer['forward'][primer].sequence)
        print('Reverse primers: ')
        for primer in reverse_primers:
            if reverse_primers[primer][0].x > 0.9:
                print(primer_index.index2primer['reverse'][primer].sequence)
                res['reverse'].append(primer_index.index2primer['reverse'][primer].sequence)
        realized_differences = np.zeros(differences.shape, dtype=np.int8)
        for pair in covered_pairs:
            if covered_pairs[pair].x > 0.9:
                realized_differences[pair[1], pair[0]] = 1
        for sequence in covered_binary:
            if covered_binary[sequence].x > 0.9:
                seqs_covered += 1/len(sequences)
        return [True, res, realized_differences, seqs_covered]
    return [False, None, None, None]

def greedy_amplicon_selection(sequences, amplicons, differences_per_amplicon, primer_width, 
                                search_width, primer_index, comparison_matrix, max_amplicons, 
                                coverage, temperature_range, beta=0.05, logging=False, 
                                output_file=None):
    '''
    Function that performs the greedy amplicon selection in order to find discriminatory amplicons with corresponding primers.

    Parameters
    ----------
    sequences : list
        List of multiple aligned sequences.
    amplicons : list
        List of Amplicon objects.
    differences_per_amplicon : np.array
        Numpy array containing 3 axes:
            amplicon
            sequence
            sequence
        where X[k,i,j] = 1 iff sequence i and j can be differentiated according to amplicon k.
    primer_width : int
        Length of primers.
    search_width : int
        Window around amplicons from which primers will be found.
    primer_index : PrimerIndex
        PrimerIndex object containing all the primers.
    comparison_matrix : dict [ (char,char) ]
        Dictionary that determines which characters should be considered equal.
    max_amplicons : int
        Number of amplicons to find.
    coverage : float
        Fraction of sequences for which binding forward and reverse primers have to be found.
    temperature_range : float
        Maximal difference between minimum primer melting temperature and maximum primer melting temperature.
    beta : float, optional
        Parameter modelling the trade-off between adding primer pairs and increasing differentiability. The default is 0.05.
    logging : bool, optional
        Boolean value that, if set to True, ensures logging of process. The default is False.
    output_file : str, optional
        Path to logfile where logging information will be APPENDED. The default is None in which case logging will not be saved.

    Returns
    -------
    result_amplicons : list
        List of Amplicon objects that were selected during greedy amplicon finding (in order of being picked).
    log_results : list
        List with logging information.
    result_primers : list
        List of primers (both fwd and rev) corresponding to amplicons in &result_amplicons

    '''
    to_cover = np.sum(differences_per_amplicon, axis=0)
    to_cover = np.sum(to_cover > 0)
    
    if logging:
        log_results = ['Total to cover based on amplicon feasibility: ' + str(to_cover) + ' with ' + str(len(sequences)) + ' sequences and ' + str(len(amplicons)) + ' amplicons.']
    
    result_amplicons = []       #this will store the result amplicons
    result_primers = []         #this will store the result primers

    amplicons.sort(key = lambda x : np.sum(differences_per_amplicon[x.id_num]), reverse=True) #sort based on differentiability
    while to_cover > 0 and len(result_amplicons) < max_amplicons and len(amplicons) > 0:
        best_amplicon = amplicons.pop(0)

        if logging:
            log_results.append('Checking amplicon: ' + str(best_amplicon.id))
        primer_index.check_amplicon(sequences, best_amplicon, primer_width, search_width)
        result_amplicons.append(best_amplicon)

        #Check if current amplicon can be added based on primer feasibility
        if coverage >= 1:
            [check, cur_primers, covered_differences, sequences_covered] = check_primer_feasibility_single_amplicon_full_coverage(sequences, best_amplicon, differences_per_amplicon[best_amplicon.id_num], primer_index, temperature_range=temperature_range, feasibility_check=True)
        else:
            [check, cur_primers, covered_differences, sequences_covered] = check_primer_feasibility_single_amplicon_variable_coverage(sequences, best_amplicon, differences_per_amplicon[best_amplicon.id_num], np.sum(differences_per_amplicon[best_amplicon.id_num]), primer_index, temperature_range=temperature_range, beta=beta, coverage=coverage)
        if check:
            if coverage >= 1:
                [_, cur_primers, covered_differences, sequences_covered] = check_primer_feasibility_single_amplicon_full_coverage(sequences, best_amplicon, differences_per_amplicon[best_amplicon.id_num], primer_index, temperature_range=temperature_range, feasibility_check=False)
            to_cover = to_cover - np.sum(covered_differences)
            if logging:
                log_results.append('Amplicon ' + str(best_amplicon.id) + ' succesfully added, new sequence pairs covered: ' + str(np.sum(covered_differences)) + '(fraction differences covered: ' + str(np.sum(covered_differences)/np.sum(differences_per_amplicon[best_amplicon.id_num])) + '), (fraction sequences covered: ' + str(sequences_covered) + ')')
                if output_file:
                    with open(output_file, 'a') as f:
                        cur_count = 1
                        for primer in cur_primers['forward']:
                            f.write('>AMPLICON_' + str(len(result_amplicons)) + '_F' + str(cur_count) +'\n')
                            f.write(primer + '\n')
                            cur_count += 1
                        cur_count = 1
                        for primer in cur_primers['reverse']:
                            f.write('>AMPLICON_' + str(len(result_amplicons)) + '_R' + str(cur_count) + '\n')
                            f.write(primer + '\n')
                            cur_count += 1
            for amplicon in amplicons:
                differences_per_amplicon[amplicon.id_num][covered_differences == 1] = 0
            amplicons = [a for a in amplicons if np.sum(differences_per_amplicon[a.id_num]) > 0]
            amplicons.sort(key = lambda x : np.sum(differences_per_amplicon[x.id_num]), reverse=True)
            result_primers.append(cur_primers)
        else:
            result_amplicons.pop(-1)
            if logging:
                log_results.append('Amplicon ' + str(best_amplicon.id) + ' rejected due to being unable to find primers to cover enough sequences')
    if logging:
        return log_results, result_amplicons, result_primers
    else:
        return result_amplicons