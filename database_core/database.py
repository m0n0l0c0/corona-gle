from threading import Thread
from collections import defaultdict, OrderedDict
import schedule
import time
import os
import glob
import glob2
import json
from tqdm import tqdm
import concurrent.futures as cf
from functools import partial
import pickle
from bson.binary import Binary
import datetime
import pandas as pd
import math

from . import Params
from . import Connection
from .utils import fix_doi, clean_text, remove_title_numbers
#from section_translator import SectionTranslator

class Database:
    """
    ==============================================================================
    ==============================================================================
        DDBB METHODS
    ==============================================================================
    ==============================================================================
    """
    """
    ==============================================================================
        CACHE
    ==============================================================================
    """
    CACHE = {
    }
    def add_cache(name, data, valid=None):
        if valid is None:
            valid = datetime.timedelta(hours=12)
        Database.CACHE[name] = {
            'data': data,
            'valid': datetime.datetime.now() + valid
        }

    def get_cache(name):
        data = Database.CACHE.get(name, None)
        if data['valid'] <= datetime.datetime.now():
            return data['data']
        else:
            return None

    """
    ==============================================================================
        METHODS HOOK
    ==============================================================================
    """
    METHODS = {}
    @staticmethod
    def register_method(class_method):
        if hasattr(class_method, 'NAME'):
            name = getattr(class_method, 'NAME')
        else:
            name = class_method.__class__.__name__
        assert('.' not in name and '$' not in name)
        Database.METHODS[name] = {
            'class': class_method,
            'init': False
        }

    @staticmethod
    def get_method(name):
        assert('.' not in name and '$' not in name)
        class_dict = Database.METHODS[name]
        method_obj = class_dict['class']
        if not class_dict['init']:
            method_obj.init()
            class_dict['init'] = True
        return method_obj

    @staticmethod
    def list_methods():
        return list(Database.METHODS.keys())

    """
    ==============================================================================
        INSERTION
    ==============================================================================
    """
    @staticmethod
    def exists(hash_id):
        return Connection.DB.documents.find_one({'hash_id': hash_id}) is not None

    @staticmethod
    def format_document_from_raw(raw_document):
        document = {
            'cord_uid': raw_document['cord_uid'],
            'doi': raw_document['doi'],
            'publish_time': raw_document['publish_time'],
            'source': raw_document['source'],
            
            'hash_id': raw_document['hash_id'],
            'title': raw_document['title'],
            'url': raw_document['url'],
            'clean': {
                #sections: ...
                # citations: ...
            },
            'raw': {
                'authors': raw_document['authors'],
                'sections': raw_document['sections'],
                'citations': raw_document['citations'],
                'bib_entries': raw_document['bib_entries'],
                'ref_entries': raw_document['ref_entries']
            },
            'sections_order': raw_document['sections_order'],
            'sections_embeddings': {
                # algorithm:
                #   word2vec: 
                #       abstract: {
                #           vector: ...
                #           num_elements: ...
                #       }
            },
            'entities': {
                # words:
                # embeddings
            },
            'topics': None, # embeddings,
            'sections_translation': {
                # section -> {#method, #abstract, #conclusions, #results, #acks, #references}
            }
        }

        return document

    @staticmethod
    def insert_raw_documents(raw_documents):
        """
            raw_documents: list of raw_documents

        """
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                documents = [Database.format_document_from_raw(doc) for doc in raw_documents]
                Connection.DB.documents.insert_many(documents)

    @staticmethod
    def insert_raw_document(raw_document):
        """
            raw_document: dict

        """
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                doc = Database.format_document_from_raw(raw_document)
                Connection.DB.documents.insert_one(doc)

    """
    ==============================================================================
        UPDATE
    ==============================================================================
    """
    @staticmethod
    def update_raw_documents(raw_documents):
        """
            raw_documents: dict
                - hash_id
                - sections
                - citations

        """
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                for doc in raw_documents:
                    Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {'raw': doc}}, upsert=True)

    @staticmethod
    def update_clean_documents(clean_documents):
        """
            clean_document: dict
                - hash_id
                - sections
                - citations

        """
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                for doc in clean_documents:
                    Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {'clean': doc}}, upsert=True)

    @staticmethod
    def update_fields_documents(documents, fields):
        if not isinstance(fields, list):
            fields = [fields]

        """ Use with caution """
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                for doc in documents:
                    Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {field: doc[field] for field in fields}}, upsert=True)

    @staticmethod
    def fix_compute_mean_vector(use, func, doc):
        return func(doc[use])

    @staticmethod
    def update_mean_vectors(method, use='raw', force=False):
        assert('.' not in method and '$' not in method)
        method_obj = Database.get_method(method)

        if not force:
            query_dict = {f'sections_embeddings.{method}': {'$exists': False}}
        else:
            query_dict = {}
        documents = Database.list_documents(query=query_dict, projection={use: 1, 'hash_id': 1, '_id': 0, f'sections_embeddings.{method}': 1})

        num_workers = Params.COMPUTE_VECTORS_WORKERS if not hasattr(method_obj, 'NUM_WORKERS') else method_obj.NUM_WORKERS
        use_loop = False
        if hasattr(method_obj, 'TYPE_THREADING'):
            if method_obj.TYPE_THREADING == 'pytorch':
                import torch.multiprocessing as mp
                try:
                    mp.set_start_method('spawn', True)
                except:
                    pass
                create_exec = lambda: mp.Pool(num_workers)
            
            elif method_obj.TYPE_THREADING == 'python':
                from multiprocessing import Pool
                create_exec = lambda: Pool(num_workers)

            elif method_obj.TYPE_THREADING == None:
                use_loop = True

            else:
                create_exec = lambda: cf.ThreadPoolExecutor(max_workers=num_workers)
        
        else:
            create_exec = lambda: cf.ThreadPoolExecutor(max_workers=num_workers)

        if use_loop:
            for doc in tqdm(documents):
                sections_vector = method_obj.compute_mean_vector(doc[use])
                try:
                    with Connection.CLIENT.start_session() as session:
                        with session.start_transaction():
                            if force or method not in doc['sections_embeddings'].keys():
                                for k in sections_vector.keys():
                                    if sections_vector[k] is not None:
                                        sections_vector[k]['vector'] = Binary(pickle.dumps(sections_vector[k]['vector'], protocol=2))
                                Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {f'sections_embeddings.{method}': sections_vector}}, upsert=True)
                except:
                    pass
        else: 
            with create_exec() as executor:
                for doc, sections_vector in zip(documents, tqdm(executor.map(partial(Database.fix_compute_mean_vector, use, method_obj.compute_mean_vector), documents), total=len(documents))):
                    with Connection.CLIENT.start_session() as session:
                        with session.start_transaction():
                            if force or method not in doc['sections_embeddings'].keys():
                                for k in sections_vector.keys():
                                    if sections_vector[k] is not None:
                                        sections_vector[k]['vector'] = Binary(pickle.dumps(sections_vector[k]['vector'], protocol=2))
                                Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {f'sections_embeddings.{method}': sections_vector}}, upsert=True)
    
    @staticmethod
    def update_translation_sections(model_path, use='raw', force=False):
        import torch        
        import flair
        import numpy as np
        flair.devide = torch.device('cuda:0')
        from flair.models import TextClassifier
        from flair.data import Sentence, segtok_tokenizer
        from flair.embeddings import FlairEmbeddings as FlairEmbeddings, DocumentPoolEmbeddings
        
        # Define similarity
        cos_sim = torch.nn.CosineSimilarity(dim=0, eps=1e-6)

        # Define document embedding
        flair_emb = DocumentPoolEmbeddings([
                FlairEmbeddings('en-forward-fast'), 
                FlairEmbeddings('en-backward-fast')
            ],
            pooling='mean',
        )

        # Get probability of class given position
        sections_classifier_prob_length = list(Params.SECTIONS_CLASSIFIER_POSITIONS_COND_CLASS.values())[0].shape[0]
        def get_P_c_cond_pos():
            P_c_cond_pos = {}
            z_norm = np.zeros(shape=(sections_classifier_prob_length, ))
            for c in Params.SECTIONS_CLASSIFIER_POSITIONS_COND_CLASS.keys():
                P_c_cond_pos[c] = Params.SECTIONS_CLASSIFIER_PRIORS[c] * Params.SECTIONS_CLASSIFIER_POSITIONS_COND_CLASS[c]
                z_norm += P_c_cond_pos[c]

            for c in Params.SECTIONS_CLASSIFIER_POSITIONS_COND_CLASS.keys():
                P_c_cond_pos[c] = (P_c_cond_pos[c] + 1e-10) / (z_norm + 1e-10)

            return P_c_cond_pos
        sections_class_cond_positions = get_P_c_cond_pos()

        # Generate embeddings of keywords
        sections_classifier_keywords_embeddings = {}
        for k in Params.SECTIONS_CLASSIFIER_KEYWORDS.keys():
            sections_classifier_keywords_embeddings[k] = []
            for i in range(len(Params.SECTIONS_CLASSIFIER_KEYWORDS[k])):
                sentence = Sentence(Params.SECTIONS_CLASSIFIER_KEYWORDS[k][i].lower())
                flair_emb.embed(sentence)
                sections_classifier_keywords_embeddings[k].append(sentence.embedding)
                sentence.clear_embeddings()

        # Function to find the nearest section given title
        def get_near_section(mean_vector):
            max_value = -2
            max_section = None
            for possible_section, candidates in sections_classifier_keywords_embeddings.items():
                for candidate in candidates:
                    score = cos_sim(mean_vector, candidate)

                    if score > 0.9: # consideramos valido
                        if max_value < score:
                            max_value = score
                            max_section = possible_section

            if max_section is not None: # Hay seccion seleccionada
                return max_section, float(max_value.cpu().detach().numpy())
            else:
                return None, None

        # Text section classifier
        classifier = TextClassifier.load(model_path)

        if not force:
            query_dict = {'$or': [{'sections_translation': {'$exists': False}}, {'sections_translation': {'$eq': dict()}}]}
        else:
            query_dict = {}
        documents = Database.list_documents(query=query_dict, projection={use: 1, 'hash_id': 1, '_id': 1, 'raw.sections': 1})

        with torch.no_grad():
            for doc in tqdm(documents):
                #try:
                translation_lut = {}
                
                offsets = np.concatenate(([0], np.cumsum([len(section_text) for section_text in doc[use]['sections'].values()])))
                total_offsets = offsets[-1]
                for section_idx, (section_title, section_text) in enumerate(doc[use]['sections'].items()):
                    # Get the section
                    norm_position_start = offsets[section_idx] / total_offsets
                    norm_position_end = offsets[section_idx + 1] / total_offsets

                    predict_labels = None

                    # Classification using section_title
                    if predict_labels is None:
                        if section_title == "":
                            continue
                        
                        clean_title = remove_title_numbers(clean_text(section_title.lower()))
                        if clean_title is None or clean_title == "":
                            continue

                        sentence_title = Sentence(clean_title)
                        flair_emb.embed(sentence_title)
                        mean_vector = sentence_title.embedding
                        predict_label_aux, predict_score_aux = get_near_section(mean_vector)
                        sentence_title.clear_embeddings()

                        if predict_label_aux is not None:
                            other_prob = (1 - predict_score_aux) / (len(sections_classifier_keywords_embeddings.keys()) - 1)
                            predict_labels = {k:predict_score_aux if predict_label_aux == k else other_prob for k in \
                                sections_classifier_keywords_embeddings.keys()}
                    
                    # Classification using section_text
                    if predict_labels is None: 
                        if section_text == "":
                            continue

                        sentence_text = Sentence(section_text.lower(), use_tokenizer=segtok_tokenizer)
                        classifier.predict(sentence_text, multi_class_prob=True)
                        predict_labels = {l.value: l.score for l in sentence_text.labels}

                    # Normalize classification using position
                    if predict_labels is not None:
                        scaled_pos_start = int(math.floor(sections_classifier_prob_length * norm_position_start))
                        scaled_pos_end = int(math.floor(sections_classifier_prob_length * norm_position_end))
                        for label, p in predict_labels.items():
                            predict_labels[label] = p * \
                                np.median(sections_class_cond_positions[label][scaled_pos_start:scaled_pos_end])
                        
                        z_norm = sum([p for p in predict_labels.values()])
                        
                        for label in predict_labels.keys():
                            predict_labels[label] = predict_labels[label] / z_norm

                    # Insert the most probable value
                    if predict_labels is not None:
                        max_k = None
                        max_v = -1
                        for k, v in predict_labels.items():
                            if max_v < v:
                                max_v = v
                                max_k = k

                        # print(doc['_id'], section_title, section_text[:50], max_k, max_v, norm_position)
                        translation_lut[section_title] = max_k, max_v


                with Connection.CLIENT.start_session() as session:
                    with session.start_transaction():
                        Connection.DB.documents.update_one({'hash_id': doc['hash_id']}, {'$set': {'sections_translation': translation_lut}})
                #except:
                #    pass


    """
    ==============================================================================
        GET
    ==============================================================================
    """
    def list_documents(query={}, hash_ids=None, projection={}, use_translation=False):
        query_dict = {}
        if hash_ids is not None:
            query_dict['hash_id'] = {'$in': hash_ids}
        query_dict.update(query)

        if use_translation:
            projection.update({'sections_translation': 1})
        
        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                documents = []
                for doc in Connection.DB.documents.find(query_dict, projection):
                    if use_translation:
                        for type_data in ['raw', 'clean']:
                            if type_data in projection.keys() and bool(projection[type_data]):
                                aux_sections = doc[type_data]['sections']
                                doc[type_data]['sections'] = {}
                                for k in aux_sections:
                                    fix_section = doc['sections_translation'][k]
                                    doc[type_data]['sections'][fix_section] = aux_sections[k]

                    documents.append(doc)

                return documents
        return []

    def list_raw_documents(hash_ids=None, use_translation=False):
        return Database.list_documents(hash_ids=hash_ids, projection={'raw': 1, 'hash_id': 1, '_id': 0, 'title': 1, 'url': 1}, use_translation=use_translation)

    def list_clean_documents(hash_ids=None, use_translation=False):
        return Database.list_documents(hash_ids=hash_ids, projection={'clean': 1, 'hash_id': 1, '_id': 0, 'title': 1, 'url': 1}, use_translation=use_translation)

    def list_titles(hash_ids=None):
        return Database.list_documents(hash_ids=hash_ids, projection={'title': 1, 'hash_id': 1, '_id': 0}, use_translation=use_translation)

    def read_mean_embedding(method_obj, doc):
        if 'sections_embeddings' not in doc:
            mean_vector = None

        else:
            for k in doc['sections_embeddings'].keys():
                if doc['sections_embeddings'][k] is not None:
                    doc['sections_embeddings'][k]['vector'] = pickle.loads(doc['sections_embeddings'][k]['vector'])

            mean_vector = method_obj.get_mean_vector(doc['sections_embeddings'])

        return {
            'vector': mean_vector,
            'hash_id': doc['hash_id']
        }

    def list_doc_embeddings(method, hash_ids=None, cache=True):
        assert('.' not in method and '$' not in method)
        method_obj = Database.get_method(method)
        query_dict = {}
        if hash_ids is not None:
            query_dict['hash_id'] = {'$in': hash_ids}

        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                output_vectors = []

                with cf.ThreadPoolExecutor(max_workers=Params.READ_EMBEDDINGS_WORKERS) as executor:
                    list_docs = Connection.DB.documents.aggregate([
                        {'$project': {'sections_translation': 1, 'sections_embeddings': f'$sections_embeddings.{method}', 'hash_id': 1, '_id': 0}}
                    ])
                    for vec in executor.map(partial(Database.read_mean_embedding, method_obj), list_docs):
                        output_vectors.append(vec)

                return output_vectors
        return []

    def read_mean_embedding_from_section(method_obj, use_translation, doc):
        if use_translation:
            translation_lut = doc['sections_translation']
        else:
            translation_lut = None

        for k in doc['sections_embeddings'].keys():
            if doc['sections_embeddings'][k] is not None:
                doc['sections_embeddings'][k]['vector'] = pickle.loads(doc['sections_embeddings'][k]['vector'])
        
        mean_vector = method_obj.get_mean_vector_from_section(doc['sections_embeddings'], section, translation_lut)
        output_vectors.append({
            'vector': mean_vector,
            'hash_id': doc['hash_id']
        })

    def list_doc_embeddings_from_section(method, section, hash_ids=None, use_translation=False):
        assert('.' not in method and '$' not in method)
        method_obj = Database.get_method(method)
        query_dict = {}
        if hash_ids is not None:
            query_dict['hash_id'] = {'$in': hash_ids}

        with Connection.CLIENT.start_session() as session:
            with session.start_transaction():
                output_vectors = []

                with cf.ThreadPoolExecutor(max_workers=Params.READ_EMBEDDINGS_WORKERS) as executor:
                    list_docs = Connection.DB.documents.aggregate([
                        {'$project': {'sections_translation': 1, 'sections_embeddings': f'$sections_embeddings.{method}', 'hash_id': 1, '_id': 0}}
                    ])
                    for vec in executor.map(partial(Database.read_mean_embedding_from_section, method_obj, use_translation), list_docs):
                        output_vectors.append(vec)
                    
                return output_vectors
        return []
    
    """
    ==============================================================================
    ==============================================================================
        DATA INGESTION AND PROCESSING
    ==============================================================================
    ==============================================================================
    """
    """
    ==============================================================================
        SCAN AND SAVE
    ==============================================================================
    """
    @staticmethod
    def parse_document_json(metadata, json_path):
        def get_paper_from_metadata(metadata, json_data):
            paper_json_id = json_data['paper_id'].strip()
            for i, paper_ids in enumerate(metadata['sha']):
                if str(paper_ids) == "nan":
                    continue

                for paper_id in paper_ids.split(";"):
                    paper_id = paper_id.strip()
                    if paper_id == paper_json_id:
                        return i
            return None

        data = {}
        with open(json_path) as json_file:
            try:
                json_data = json.load(json_file)
            except:
                return None

            if Database.exists(json_data['paper_id']):
                return None

            idx = get_paper_from_metadata(metadata, json_data)
            if idx is not None:
                meta_paper = metadata.iloc[idx]
            else:
                return None

            data['cord_uid'] = str(meta_paper['cord_uid'])
            data['url'] = str(meta_paper['url'])
            data['doi'] = fix_doi(str(meta_paper['doi']))
            data['publish_time'] = str(meta_paper['publish_time'])
            data['source'] = str(meta_paper['source_x'])
            
            data['hash_id'] = json_data['paper_id']
            data['title'] = json_data['metadata']['title']
            data['authors'] = json_data['metadata']['authors']
            data['bib_entries'] = json_data['bib_entries']
            data['ref_entries'] = json_data['ref_entries']

            data['citations'] = defaultdict(list)
            data['sections'] = defaultdict(lambda: "")

            # Abstract
            if isinstance(json_data['abstract'], (list, tuple)):
                try:
                    data['sections']['abstract'] = json_data['abstract'][0]['text']
                    data['citations']['abstract'] += [{'start': cite['start'], 'end': cite['end'], 'ref_id': cite['ref_id']} for cite in json_data['abstract'][0]['cite_spans']]
                except:
                    data['sections']['abstract'] = ''
            else:
                data['sections']['abstract'] = json_data['abstract']

            offsets = defaultdict(lambda: 0)
            sections_order = OrderedDict()
            for block_text in json_data['body_text']:
                text = block_text['text']
                section = block_text['section'].replace('.', "").replace("$", "")
                data['sections'][section] += text
                data['citations'][section] += [{'start': offsets[section] + cite['start'], 'end': offsets[section] + cite['end'], 'ref_id': cite['ref_id']} for cite in block_text['cite_spans']]
                offsets[section] += len(text)

                if section not in sections_order:
                    sections_order[section] = True
            
            data['sections_order'] = list(sections_order.keys())
        
        return data

    @staticmethod
    def scan_file(metadata, json_path):
        return Database.parse_document_json(metadata, json_path)

    @staticmethod
    def scan_folder(metadata, folder_path):
        documents = []
        for folder_path in filter(lambda folder_path: os.path.isdir(folder_path), glob2.iglob(os.path.join(folder_path, "*"))):
            folder_name = os.path.basename(folder_path)
            print('\tProcessing %s folder' % (folder_name, ))
            with cf.ThreadPoolExecutor(max_workers=Params.SCAN_WORKERS) as executor:
                list_jsons = glob2.glob(os.path.join(folder_path, "**", "*.json"))
                for raw_doc in tqdm(executor.map(partial(Database.scan_file, metadata), list_jsons), total=len(list_jsons)):
                    if raw_doc is not None:
                        Database.insert_raw_document(raw_doc)
                        documents.append(raw_doc)

        # Return
        return documents

    """
    ==============================================================================
        SYNC
    ==============================================================================
    """
    @staticmethod
    def sync(once=False, update_database=True, callback_preprocessing=None):
        # Lazy loading to avoid asking for credentials when not syncing
        import kaggle
        is_processing = False

        def __sync_thread():
            nonlocal is_processing
            if is_processing:
                return

            is_processing = True
            print('Checking new changes...', end=' ')
            
            # Download from kaggle
            kaggle.api.authenticate()
            kaggle.api.dataset_download_files(Params.DATASET_KAGGLE_NAME, path=Params.DATASET_KAGGLE_RAW, unzip=True)
            print('Done')
            # Read csv
            metadata = pd.read_csv(os.path.join(Params.DATASET_KAGGLE_RAW, "metadata.csv"))

            # Create new dataset with the changes
            if update_database:
                raw_documents = Database.scan_folder(metadata, Params.DATASET_KAGGLE_RAW)
            else:
                raw_documents = None
            
            # Execute callback
            if callback_preprocessing is not None:
                callback_preprocessing(raw_documents)

            # Is done
            is_processing = False
        
        if once:
            __sync_thread()
            return

        t = Thread(target=__sync_thread)
        t.start()

        if daemon:
            t = Thread(target=__sync_thread)
            t.start()


    """
    ==============================================================================
        Single Field updates
    ==============================================================================
    """
    # @staticmethod
    # def update_field(fields):
    #     folder_path = Params.DATASET_KAGGLE_RAW
    #     # Database.sync(once=True, update_database=False)

    #     # Read csv
    #     metadata = pd.read_csv(os.path.join(Params.DATASET_KAGGLE_RAW, "metadata.csv"))

    #     # like scan, but updating instead of inserting
    #     documents = []
    #     for folder_path in filter(lambda folder_path: os.path.isdir(folder_path), glob2.iglob(os.path.join(folder_path, "*"))):
    #         folder_name = os.path.basename(folder_path)
    #         print('\tProcessing %s folder' % (folder_name, ))
    #         with cf.ThreadPoolExecutor(max_workers=Params.SCAN_WORKERS) as executor:
    #             list_jsons = glob2.glob(os.path.join(folder_path, "**", "*.json"))
    #             for raw_doc in tqdm(executor.map(partial(Database.scan_file, metadata), list_jsons), total=len(list_jsons)):
    #                 if raw_doc is not None:
    #                     Database.update_fields_documents([raw_doc], fields)
    #                     documents.append(raw_doc)

    #     # Return
    #     return documents
    # from database_core import Database
    # Database.update_field(['cord_uid', 'url'])
