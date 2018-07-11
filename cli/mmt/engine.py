import inspect
import json
import os
import shutil
from xml.dom import minidom

import logging

import time

import cli
from cli import IllegalArgumentException, CorpusNotFoundInFolderException, mmt_javamain
from cli.libs import osutils, nvidia_smi
from cli.mmt import BilingualCorpus

__author__ = 'Davide Caroselli'


class TMCleaner:
    def __init__(self, source_lang, target_lang):
        self._source_lang = source_lang
        self._target_lang = target_lang

        self._java_main = 'eu.modernmt.cli.CleaningPipelineMain'

    def clean(self, corpora, output_path, log=None):
        if log is None:
            log = osutils.DEVNULL

        args = ['-s', self._source_lang, '-t', self._target_lang,
                '--output', output_path, '--input']

        input_paths = set([corpus.get_folder() for corpus in corpora])

        for root in input_paths:
            args.append(root)

        extended_heap_mb = int(osutils.mem_size() * 90 / 100)

        command = mmt_javamain(self._java_main, args=args, max_heap_mb=extended_heap_mb)
        osutils.shell_exec(command, stdout=log, stderr=log)

        return BilingualCorpus.list(self._source_lang, self._target_lang, output_path)


class TrainingPreprocessor:
    DEV_FOLDER_NAME = 'dev'
    TEST_FOLDER_NAME = 'test'

    def __init__(self, source_lang, target_lang):
        self._source_lang = source_lang
        self._target_lang = target_lang

        self._java_main = 'eu.modernmt.cli.TrainingPipelineMain'

    def process(self, corpora, output_path, data_path=None, log=None):
        if log is None:
            log = osutils.DEVNULL

        args = ['-s', self._source_lang, '-t', self._target_lang, '--output', output_path, '--input']

        for root in set([corpus.get_folder() for corpus in corpora]):
            args.append(root)

        if data_path is not None:
            args.append('--dev')
            args.append(os.path.join(data_path, TrainingPreprocessor.DEV_FOLDER_NAME))
            args.append('--test')
            args.append(os.path.join(data_path, TrainingPreprocessor.TEST_FOLDER_NAME))

        command = mmt_javamain(self._java_main, args)
        osutils.shell_exec(command, stdout=log, stderr=log)

        return BilingualCorpus.list(self._source_lang, self._target_lang, output_path)


class FastAlign:
    def __init__(self, model, source_lang, target_lang):
        # FastAlign only supports base languages, without regions
        self._model = os.path.join(model, '%s__%s.mdl' % (source_lang.split('-')[0], target_lang.split('-')[0]))
        self._source_lang = source_lang
        self._target_lang = target_lang

        self._build_bin = os.path.join(cli.BIN_DIR, 'fa_build')
        self._align_bin = os.path.join(cli.BIN_DIR, 'fa_align')
        self._export_bin = os.path.join(cli.BIN_DIR, 'fa_export')

    def build(self, corpora, log=None):
        if log is None:
            log = osutils.DEVNULL

        shutil.rmtree(self._model, ignore_errors=True)
        osutils.makedirs(self._model, exist_ok=True)

        source_path = set([corpus.get_folder() for corpus in corpora])
        assert len(source_path) == 1
        source_path = source_path.pop()

        command = [self._build_bin, '-s', self._source_lang, '-t', self._target_lang, '-i', source_path,
                   '-m', self._model, '-I', '4']
        osutils.shell_exec(command, stdout=log, stderr=log)


class Engine(object):
    @staticmethod
    def _get_config_path(name):
        return os.path.join(cli.ENGINES_DIR, name, 'engine.xconf')

    @staticmethod
    def list():
        return sorted([name for name in os.listdir(cli.ENGINES_DIR) if os.path.isfile(Engine._get_config_path(name))])

    @staticmethod
    def load(name):
        if os.sep in name:
            raise IllegalArgumentException('Invalid engine name: "%s"' % name)

        config_path = Engine._get_config_path(name)

        if not os.path.isfile(config_path):
            raise IllegalArgumentException("Engine '%s' not found" % name)

        # parse the source language and target language from the configuration file
        def _get_child(root, child_name):
            elements = root.getElementsByTagName(child_name)
            return elements[0] if len(elements) > 0 else None

        languages = []

        config_root = minidom.parse(config_path).documentElement
        engine_el = _get_child(config_root, 'engine')
        lang_el = _get_child(engine_el, 'languages')

        if lang_el is not None:
            for pair_el in lang_el.getElementsByTagName('pair'):
                source_lang = pair_el.getAttribute('source')
                target_lang = pair_el.getAttribute('target')
                languages.append((source_lang, target_lang))
        else:
            source_lang = engine_el.getAttribute('source-language')
            target_lang = engine_el.getAttribute('target-language')
            languages.append((source_lang, target_lang))

        return Engine(name, languages)

    def __init__(self, name, languages):
        # properties
        self.name = name if name is not None else 'default'
        self.languages = languages

        # base paths
        self.config_path = self._get_config_path(self.name)
        self.path = os.path.join(cli.ENGINES_DIR, name)
        self.data_path = os.path.join(self.path, 'data')
        self.models_path = os.path.join(self.path, 'models')
        self.runtime_path = os.path.join(cli.RUNTIME_DIR, self.name)
        self.logs_path = os.path.join(self.runtime_path, 'logs')
        self.temp_path = os.path.join(self.runtime_path, 'tmp')

    def exists(self):
        return os.path.isfile(self.config_path)

    def get_logfile(self, name, ensure=True, append=False):
        if ensure and not os.path.isdir(self.logs_path):
            osutils.makedirs(self.logs_path, exist_ok=True)

        logfile = os.path.join(self.logs_path, name + '.log')

        if not append and ensure and os.path.isfile(logfile):
            os.remove(logfile)

        return logfile

    def get_tempdir(self, name, ensure=True):
        if ensure and not os.path.isdir(self.temp_path):
            osutils.makedirs(self.temp_path, exist_ok=True)

        folder = os.path.join(self.temp_path, name)

        if ensure:
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder)

        return folder

    def get_tempfile(self, name, ensure=True):
        if ensure and not os.path.isdir(self.temp_path):
            osutils.makedirs(self.temp_path, exist_ok=True)
        return os.path.join(self.temp_path, name)

    def clear_tempdir(self, subdir=None):
        path = os.path.join(self.temp_path, subdir) if subdir is not None else self.temp_path
        shutil.rmtree(path, ignore_errors=True)


class EngineBuilder:
    _MB = (1024 * 1024)
    _GB = (1024 * 1024 * 1024)

    class Step:
        def __init__(self, name, optional=True, hidden=False):
            self._name = name
            self._optional = optional
            self._hidden = hidden

        def __call__(self, *_args, **_kwargs):
            class _Inner:
                def __init__(self, f, name, optional, hidden):
                    self.id = f.__name__.strip('_')
                    self.name = name

                    self._optional = optional
                    self._hidden = hidden
                    self._f = f

                def is_optional(self):
                    return self._optional

                def is_hidden(self):
                    return self._hidden

                def __call__(self, *args, **kwargs):
                    names, _, _, _ = inspect.getargspec(self._f)

                    if 'delete_on_exit' not in names:
                        del kwargs['delete_on_exit']
                    if 'log' not in names:
                        del kwargs['log']
                    if 'skip' not in names:
                        del kwargs['skip']

                    self._f(*args, **kwargs)

            return _Inner(_args[0], self._name, self._optional, self._hidden)

    class __Args(object):
        def __init__(self):
            pass

        def __getattr__(self, item):
            return self.__dict__[item] if item in self.__dict__ else None

        def __setattr__(self, key, value):
            self.__dict__[key] = value

    class __Schedule:
        def __init__(self, plan, filtered_steps=None):
            self._plan = plan
            self._passed_steps = []

            all_steps = self.all_steps()

            if filtered_steps is not None:
                self._scheduled_steps = filtered_steps

                unknown_steps = [step for step in self._scheduled_steps if step not in all_steps]
                if len(unknown_steps) > 0:
                    raise IllegalArgumentException('Unknown training steps: ' + str(unknown_steps))
            else:
                self._scheduled_steps = all_steps

        def __len__(self):
            return len(self._scheduled_steps)

        def __iter__(self):
            class __Inner:
                def __init__(self, plan):
                    self._plan = plan
                    self._idx = 0

                def next(self):
                    if self._idx < len(self._plan):
                        self._idx += 1
                        return self._plan[self._idx - 1]
                    else:
                        raise StopIteration

            return __Inner([el for el in self._plan if el.id in self._scheduled_steps or not el.is_optional()])

        def visible_steps(self):
            return [x.id for x in self._plan if x.id in self._scheduled_steps and not x.is_hidden()]

        def all_steps(self):
            return [e.id for e in self._plan]

        def store(self, path):
            with open(path, 'w') as json_file:
                json.dump(self._passed_steps, json_file)

        def load(self, path):
            try:
                with open(path) as json_file:
                    self._passed_steps = json.load(json_file)
            except IOError:
                self._passed_steps = []

        def step_completed(self, step):
            self._passed_steps.append(step)

        def is_completed(self, step):
            return step in self._passed_steps

    def __init__(self, engine_name, source_lang, target_lang, roots, debug=False, steps=None, split_train=True):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.roots = roots

        self._engine = Engine(engine_name, [(source_lang, target_lang)])
        self._delete_on_exit = not debug
        self._split_train = split_train

        self._temp_dir = None

        self._schedule = EngineBuilder.__Schedule(self._build_schedule(), steps)
        self._cleaner = TMCleaner(self.source_lang, self.target_lang)
        self._training_preprocessor = TrainingPreprocessor(self.source_lang, self.target_lang)
        self._aligner = FastAlign(os.path.join(self._engine.models_path, 'aligner'), self.source_lang, self.target_lang)

    def _build_schedule(self):
        return [self._clean_tms, self._preprocess, self._train_aligner, self._write_config]

    def _get_tempdir(self, name, delete_if_exists=False):
        path = os.path.join(self._temp_dir, name)
        if delete_if_exists:
            shutil.rmtree(path, ignore_errors=True)
        if not os.path.isdir(path):
            osutils.makedirs(path, exist_ok=True)
        return path

    # ~~~~~~~~~~~~~~~~~~~~~~~~~~ Engine creation management ~~~~~~~~~~~~~~~~~~~~~~~~~~

    def build(self):
        self._build(resume=False)

    def resume(self):
        self._build(resume=True)

    def _build(self, resume):
        self._temp_dir = self._engine.get_tempdir('training', ensure=(not resume))

        checkpoint_path = os.path.join(self._temp_dir, 'checkpoint.json')
        if resume:
            self._schedule.load(checkpoint_path)
        else:
            self._schedule.store(checkpoint_path)

        corpora = BilingualCorpus.list(self.source_lang, self.target_lang, self.roots)

        if len(corpora) == 0:
            raise CorpusNotFoundInFolderException('Could not find %s > %s corpora in path %s' %
                                                  (self.source_lang, self.target_lang, ', '.join(self.roots)))

        # if no old engines (i.e. engine folders) can be found, create a new one from scratch
        # if we are not trying to resume an old one, create from scratch anyway
        if not os.path.isdir(self._engine.path) or not resume:
            shutil.rmtree(self._engine.path, ignore_errors=True)
            os.makedirs(self._engine.path)

        # Create a new logger for the building activities,
        log_file = self._engine.get_logfile('training', append=resume)
        log_stream = open(log_file, 'ab' if resume else 'wb')
        logging.basicConfig(format='%(asctime)-15s [%(levelname)s] - %(message)s',
                            level=logging.DEBUG, stream=log_stream)
        logger = logging.getLogger('EngineBuilder')

        # Start the engine building (training) phases

        steps_count = len(self._schedule.visible_steps())
        log_line_len = 70

        try:
            logger.log(logging.INFO, 'Training started: engine=%s, corpora=%d, lang_pair=%s-%s' %
                       (self._engine.name, len(corpora), self.source_lang, self.target_lang))

            print '\n=========== TRAINING STARTED ===========\n'
            print 'ENGINE:  %s' % self._engine.name
            print 'CORPORA: %d corpora' % len(corpora)
            print 'LANGS:   %s > %s' % (self.source_lang, self.target_lang)
            print

            # Check if all requirements are fulfilled before actual engine training
            try:
                self._check_constraints()
            except EngineBuilder.HWConstraintViolated as e:
                print '\033[91mWARNING\033[0m: %s\n' % e.cause

            args = EngineBuilder.__Args()
            args.corpora = corpora

            # ~~~~~~~~~~~~~~~~~~~~~ RUN ALL STEPS ~~~~~~~~~~~~~~~~~~~~~
            # Note: if resume is true, a step is only run if it was not in the previous attempt

            step_index = 1

            for method in self._schedule:
                if not method.is_hidden():
                    print ('INFO: (%d of %d) %s... ' % (step_index, steps_count, method.name)).ljust(log_line_len)

                skip = self._schedule.is_completed(method.id)
                self._step_start_time = time.time()

                logger.log(logging.INFO, 'Training step "%s" (%d/%d) started' %
                           (method.id, step_index, len(self._schedule)))

                start_time = time.time()
                method(self, args, skip=skip, log=log_stream, delete_on_exit=self._delete_on_exit)
                elapsed_time_str = self._pretty_print_time(time.time() - start_time)

                if not method.is_hidden():
                    print 'DONE (in %s)' % elapsed_time_str

                logger.log(logging.INFO, 'Training step "%s" completed in %s' % (method.id, elapsed_time_str))

                self._schedule.step_completed(method.id)
                self._schedule.store(checkpoint_path)

                step_index += 1

            print '\n=========== TRAINING SUCCESS ===========\n'
            print 'You can now start, stop or check the status of the server with command:'
            print '\t./mmt start|stop|status ' + ('' if self._engine.name == 'default' else '-e %s' % self._engine.name)
            print

            if self._delete_on_exit:
                self._engine.clear_tempdir('training')
        except Exception:
            logger.exception('Unexpected exception')
            raise
        finally:
            log_stream.close()

    @staticmethod
    def _pretty_print_time(elapsed):
        elapsed = int(elapsed)
        parts = []

        if elapsed > 86400:  # days
            d = int(elapsed / 86400)
            elapsed -= d * 86400
            parts.append('%dd' % d)
        if elapsed > 3600:  # hours
            h = int(elapsed / 3600)
            elapsed -= h * 3600
            parts.append('%dh' % h)
        if elapsed > 60:  # minutes
            m = int(elapsed / 60)
            elapsed -= m * 60
            parts.append('%dm' % m)
        parts.append('%ds' % elapsed)

        return ' '.join(parts)

    class HWConstraintViolated(Exception):
        def __init__(self, cause):
            self.cause = cause

    def _check_constraints(self):
        gpus = nvidia_smi.list_gpus()
        if len(gpus) == 0:
            raise EngineBuilder.HWConstraintViolated(
                'No GPU for Neural engine training, the process will take very long time to complete.')

        recommended_gpu_ram = 8 * self._GB

        for gpu in gpus:
            gpus_ram = nvidia_smi.get_ram(gpu)

            if gpus_ram < recommended_gpu_ram:
                raise EngineBuilder.HWConstraintViolated(
                    'The RAM of GPU %d is only %.fG. More than %.fG of RAM recommended for each GPU.' %
                    (gpu, gpus_ram / self._GB, recommended_gpu_ram / self._GB))

    # ~~~~~~~~~~~~~~~~~~~~~ Training step functions ~~~~~~~~~~~~~~~~~~~~~

    @Step('Corpora cleaning')
    def _clean_tms(self, args, skip=False, log=None):
        folder = self._get_tempdir('clean_corpora')

        if skip:
            args.corpora = BilingualCorpus.list(self.source_lang, self.target_lang, folder)
        else:
            args.corpora = self._cleaner.clean(args.corpora, folder, log=log)

    @Step('Corpora pre-processing')
    def _preprocess(self, args, skip=False, log=None):
        preprocessed_folder = self._get_tempdir('preprocessed_corpora')

        if skip:
            args.processed_corpora = BilingualCorpus.list(self.source_lang, self.target_lang, preprocessed_folder)
        else:
            if not args.corpora:
                raise CorpusNotFoundInFolderException('Could not find any valid %s > %s segments in your input.' %
                                                      (self.source_lang, self.target_lang))

            data_path = self._engine.data_path if self._split_train else None
            args.processed_corpora = self._training_preprocessor.process(args.corpora, preprocessed_folder,
                                                                         data_path=data_path, log=log)

    @Step('Aligner training')
    def _train_aligner(self, args, skip=False, log=None):
        if not skip:
            corpora = filter(None, [args.processed_corpora, args.corpora])[0]
            self._aligner.build(corpora, log=log)

    @Step('Writing config', optional=False, hidden=True)
    def _write_config(self, _):
        xml_template = \
            '<node xsi:schemaLocation="http://www.modernmt.eu/schema/config mmt-config-1.0.xsd"\n' \
            '      xmlns="http://www.modernmt.eu/schema/config"\n' \
            '      xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n' \
            '   <engine source-language="%s" target-language="%s" />\n' \
            '</node>'

        with open(self._engine.config_path, 'wb') as out:
            out.write(xml_template % (self.source_lang, self.target_lang))
