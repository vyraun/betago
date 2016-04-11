from __future__ import print_function
import os
import os.path
import zipfile
import goboard
import argparse
import multiprocessing
from os import sys
from gomill import sgf as gosgf
from index_processor import KGSIndex
from train_test_split import Sampler
import numpy as np


def worker(jobinfo):
    try:
        clazz, dir_name, num_planes, zip_file, data_file_name, game_list = jobinfo
        clazz(dir_name, num_planes).process_zip(dir_name, zip_file, data_file_name, game_list)
    except (KeyboardInterrupt, SystemExit):
        raise('>>> Exiting child process.')


class GoBaseProcessor(object):

    def __init__(self, data_directory='data', num_planes=7):
        self.data_dir = data_directory
        self.num_planes = num_planes

    def process_zip(self, dir_name, zip_file_name, data_file_name, game_list):
        return NotImplemented

    def consolidate_games(self, name, samples):
        return NotImplemented

    def load_go_data(self):
        '''
        Main method for loading Go game data.
        Data types to choose from are:
            test: Load test data. This set is fixed and not contained in any train data.
            train10k: Load train data for 10000 games
            trainall: Load train data for all available games
        '''
        # Parse command line arguments
        parser = argparse.ArgumentParser(description='Process Go Game data')
        parser.add_argument('--type', dest='type', type=str, nargs='+',
                            help='Choose data type from test and train')
        parser.add_argument('--data', dest='data_dir', type=str, nargs='?',
                            help='Optional data directory')
        parser.add_argument('--n', dest='num_samples', type=int, nargs='?',
                            help='Number of samples used in training. If none provided, load full data set')
        args = parser.parse_args()

        # Determine data types for training
        types = args.type
        for t in types:
            if t not in ['test', 'train']:
                raise("""Unsupported type, choose from 'test', 'train' """)

        # Determine number of training samples
        num_samples = None if args.num_samples is None else args.num_samples

        # Determine local directory to put all data
        target_dir = 'data' if args.data_dir is None else args.data_dir
        if not os.path.isdir(target_dir):
            os.makedirs(target_dir)
        self.data_dir = target_dir

        # Load and initialize an index from KGS and download zipped files
        index = KGSIndex(data_directory=target_dir)
        index.download_files()

        # Depending on type, unzip and process data, then store it in one consolidated file.
        for name in types:
            sampler = Sampler(data_dir=target_dir)
            if name == 'test':
                samples = sampler.test_games
            elif name == 'train' and num_samples is not None:
                samples = sampler.draw_training_samples(num_samples)
            elif name == 'train' and num_samples is None:
                samples = sampler.draw_all_training()

            # Map load to CPUs, then consolidate all examples
            self.map_to_workers(name, samples)
            self.consolidate_games(name, samples)
        print('>>> Finished processing')

    def get_handicap(self, go_board, sgf):
        first_move_done = False
        if sgf.get_handicap() != None and sgf.get_handicap() != 0:
            for setup in sgf.get_root().get_setup_stones():
                for move in setup:
                    go_board.apply_move('b', move)
            first_move_done = True
        return go_board, first_move_done

    def map_to_workers(self, name, samples):
        '''
        Determine the list of zip files that need to be processed, then map load
        to number of available CPUs.
        '''
        # Create a dictionary with file names as keys and games as values
        zip_names = set()
        indices_by_zip_name = {}
        for filename, index in samples:
            zip_names.add(filename)
            if filename not in indices_by_zip_name:
                indices_by_zip_name[filename] = []
            indices_by_zip_name[filename].append(index)
        print('>>> Number of zip files: ' + str(len(zip_names)))

        # Transform the above dictionary to a list that can be processed in parallel
        zips_to_process = []
        for zip_name in zip_names:
            base_name = zip_name.replace('.zip', '')
            data_file_name = base_name + name
            if not os.path.isfile(self.data_dir + '/' + data_file_name):
                zips_to_process.append((self.__class__, self.data_dir, self.num_planes, zip_name,
                                        data_file_name, indices_by_zip_name[zip_name]))

        # Determine number of CPU cores and split work load among them
        cores = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(processes=cores)
        p = pool.map_async(worker, zips_to_process)
        try:
            results = p.get(0xFFFF)
            print(results)
        except KeyboardInterrupt:
            print("Caught KeyboardInterrupt, terminating workers")
            pool.terminate()
            pool.join()
            sys.exit(-1)

    def init_go_board(self, sgf_contents):
        sgf = gosgf.Sgf_game.from_string(sgf_contents)
        return sgf, goboard.GoBoard(19)

    def num_total_examples(self, this_zip, game_list, name_list):
        total_examples = 0
        for index in game_list:
            name = name_list[index + 1]
            if name.endswith('.sgf'):
                content = this_zip.read(name)
                sgf, go_board_no_handy = self.init_go_board(content)
                go_board, first_move_done = self.get_handicap(go_board_no_handy, sgf)

                num_moves = 0
                for item in sgf.main_sequence_iter():
                    color, move = item.get_move()
                    if color is not None and move is not None:
                        if first_move_done:
                            num_moves = num_moves + 1
                        first_move_done = True
                total_examples = total_examples + num_moves
            else:
                raise(name + ' is not a valid sgf')
        return total_examples


class GoDataProcessor(GoBaseProcessor):

    def __init__(self, data_directory='data', num_planes=7):
        super(GoDataProcessor, self).__init__(data_directory=data_directory, num_planes=num_planes)

    def feature_and_label(self, color, move, go_board):
        return NotImplemented

    def process_zip(self, dir_name, zip_file_name, data_file_name, game_list):
        # Read zipped file and extract name list
        this_zip = zipfile.ZipFile(dir_name + '/' + zip_file_name)
        name_list = this_zip.namelist()

        # Determine number of examples
        total_examples = self.num_total_examples(this_zip, game_list, name_list)

        features = np.zeros((total_examples, self.num_planes, 19, 19))
        labels = np.zeros((total_examples,))

        counter = 0
        for index in game_list:
            name = name_list[index + 1]
            if name.endswith('.sgf'):
                '''
                Load Go board and determine handicap of game, then iterate through all moves,
                store preprocessed move in data_file and apply move to board.
                '''
                sgf_content = this_zip.read(name)
                sgf, go_board_no_handy = self.init_go_board(sgf_content)
                go_board, first_move_done = self.get_handicap(go_board_no_handy, sgf)
                for item in sgf.main_sequence_iter():
                    color, move = item.get_move()
                    if color is not None and move is not None:
                        row, col = move
                        if first_move_done:
                            X, y = self.feature_and_label(color, move, go_board, self.num_planes)
                            features[counter] = X
                            labels[counter] = y
                            counter += 1
                        go_board.apply_move(color, (row, col))
                        first_move_done = True
            else:
                raise(name + ' is not a valid sgf')

        feature_file = dir_name + '/' + data_file_name + '_features'
        label_file = dir_name + '/' + data_file_name + '_labels'

        np.save(feature_file, features)
        np.save(label_file, labels)

    def consolidate_games(self, name, samples):
        print('>>> Creating consolidated numpy arrays')
        features = None
        labels = None

        files_needed = set(file_name for file_name, index in samples)
        print('>>> Total number of files: ' + str(len(files_needed)))

        file_names = []
        for zip_file_name in files_needed:
            file_name = zip_file_name.replace('.zip', '') + name
            file_names.append(file_name)

        for file_name in file_names:
            X = np.load(self.data_dir + '/' + file_name + '_features.npy')
            y = np.load(self.data_dir + '/' + file_name + '_labels.npy')
            if features is None:
                features = X
                labels = y
            else:
                features = np.concatenate((features, X), axis=0)
                labels = np.concatenate((labels, y), axis=0)

        feature_file = self.data_dir + '/' + str(self.num_planes) + '_plane_features_' + name
        label_file = self.data_dir + '/' + str(self.num_planes) + '_plane_labels_' + name

        np.save(feature_file, features)
        np.save(label_file, labels)

        return features, labels


class GoFileProcessor(GoBaseProcessor):

    def __init__(self, data_directory='data', num_planes=7):
        super(GoFileProcessor, self).__init__(data_directory=data_directory, num_planes=num_planes)

    def store_results(self, data_file, color, move, go_board):
        return NotImplemented

    def write_file_header(self, data_file, n, num_planes, board_size, bits_per_pixel):
        headerLine = 'mlv2'
        headerLine = headerLine + '-n=' + str(n)
        headerLine = headerLine + '-num_planes=' + str(num_planes)
        headerLine = headerLine + '-imagewidth=' + str(board_size)
        headerLine = headerLine + '-imageheight=' + str(board_size)
        headerLine = headerLine + '-datatype=int'
        headerLine = headerLine + '-bpp=' + str(bits_per_pixel)
        print(headerLine)
        headerLine = headerLine + "\0\n"
        headerLine = headerLine + chr(0) * (1024-len(headerLine))
        data_file.write(headerLine)

    def process_zip(self, dir_name, zip_file_name, data_file_name, game_list):
        # Read zipped file and extract name list
        this_zip = zipfile.ZipFile(dir_name + '/' + zip_file_name)
        name_list = this_zip.namelist()

        # Determine number of examples
        total_examples = self.num_total_examples(this_zip, game_list, name_list)
        print('>>> Total number of Go games in this zip: ' + str(total_examples))

        # Write file header
        data_file = open(dir_name + '/' + data_file_name, 'wb')
        self.write_file_header(data_file=data_file, n=total_examples, num_planes=7, board_size=19, bits_per_pixel=1)

        # Write body and close file
        for index in game_list:
            name = name_list[index + 1]
            if name.endswith('.sgf'):
                '''
                Load Go board and determine handicap of game, then iterate through all moves,
                store preprocessed move in data_file and apply move to board.
                '''
                sgf_content = this_zip.read(name)
                sgf, go_board_no_handy = self.init_go_board(sgf_content)
                go_board, first_move_done = self.get_handicap(go_board_no_handy, sgf)
                move_idx = 0
                for item in sgf.main_sequence_iter():
                    (color, move) = item.get_move()
                    if color is not None and move is not None:
                        row, col = move
                        if first_move_done:
                            self.store_results(data_file, color, move, go_board)
                        go_board.apply_move(color, (row, col))
                        move_idx = move_idx + 1
                        first_move_done = True
            else:
                raise(name + ' is not a valid sgf')
        data_file.write('END')
        data_file.close()

    def consolidate_games(self, name, samples):
        print('>>> Creating consolidated .dat...')
        file_path = self.data_dir + '/kgsgo_' + name
        if os.path.isfile(file_path):
            print('>>> File ' + file_path + ' already exists')
            return

        files_needed = set(file_name for file_name, index in samples)
        print('>>> Total dat files to be consolidated: ' + str(len(files_needed)))

        # Collect names of data files
        data_file_names = []
        for zip_file_name in files_needed:
            data_file_name = zip_file_name.replace('.zip', '') + name
            data_file_names.append(data_file_name)

        # Count total number of moves
        num_records = 0
        for data_file_name in data_file_names:
            if not os.path.isfile(self.data_dir + '/' + data_file_name):
                print('>>> Missing file: ' + data_file_name)
                sys.exit(-1)
            child = open(self.data_dir + '/' + data_file_name, 'rb')
            header = child.read(1024)
            this_n = int(header.split('-n=')[1].split('-')[0])
            child.close()
            num_records = num_records + this_n

        # Write content to consolidate file
        consolidated_file = open(file_path, 'wb')
        self.write_file_header(consolidated_file, num_records, self.num_planes, 19, 1)
        for filename in data_file_names:
            print('>>> Reading from ' + filename + ' ...')
            file_path = self.data_dir + '/' + filename
            single_dat = open(file_path, 'rb')
            single_dat.read(1024)
            data = single_dat.read()
            if data[-3:] != 'END':
                raise('Invalid file, doesnt end with END: ' + file_path)
            consolidated_file.write(data[:-3])
            single_dat.close()
        consolidated_file.write('END')
        consolidated_file.close()
