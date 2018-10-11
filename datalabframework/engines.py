import os

from jinja2 import Template

from . import params
from . import data
from . import utils
from . import project
import elasticsearch.helpers
import json
import pandas

# purpose of engines
# abstract engine init, data read and data write
# and move this information to metadata

# it does not make the code fully engine agnostic though.

engines = dict()

class SparkEngine():
    def __init__(self, name, config):
        from pyspark import SparkContext, SparkConf
        from pyspark.sql import SQLContext

        here = os.path.dirname(os.path.realpath(__file__))

        submit_args = ''

        jars = []
        jars += config.get('jars', [])
        if jars:
            submit_jars = ' '.join(jars)
            submit_args = '{} --jars {}'.format(submit_args, submit_jars)

        packages = config.get('packages', [])
        if packages:
            submit_packages = ','.join(packages)
            submit_args = '{} --packages {}'.format(submit_args, submit_packages)

        submit_args = '{} pyspark-shell'.format(submit_args)

        # os.environ['PYSPARK_SUBMIT_ARGS'] = "--packages org.postgresql:postgresql:42.2.5 pyspark-shell"
        os.environ['PYSPARK_SUBMIT_ARGS'] = submit_args
        print('PYSPARK_SUBMIT_ARGS: {}'.format(submit_args))

        conf = SparkConf()
        if 'jobname' in config:
            conf.setAppName(config.get('jobname'))

        md = params.metadata()['providers']
        for v in md.values():
            if v['service'] == 'minio':
                conf.set("spark.hadoop.fs.s3a.endpoint", 'http://{}:{}'.format(v['hostname'],v.get('port',9000))) \
                    .set("spark.hadoop.fs.s3a.access.key", v['access']) \
                    .set("spark.hadoop.fs.s3a.secret.key", v['secret']) \
                    .set("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
                    .set("spark.hadoop.fs.s3a.path.style.access", True)
                break

        conf.setMaster(config.get('master', 'local[*]'))
        self._ctx = SQLContext(SparkContext(conf=conf))
        self.info = {'name': name, 'context':'spark', 'config': config}

    def context(self):
        return self._ctx

    def read(self, resource=None, path=None, provider=None, **kargs):
        md = data.metadata(resource, path, provider)
        if not md:
            print('no valid resource found')
            return

        pd = md['provider']

        cache = pd.get('read',{}).get('cache', False)
        cache = md.get('read',{}).get('cache', cache)
        
        repartition = pd.get('read',{}).get('repartition', None)
        repartition = pd.get('read',{}).get('repartition', repartition)
        
        coalesce = pd.get('read',{}).get('coalesce', None)
        coalesce = md.get('read',{}).get('coalesce', coalesce)

        print('repartition ', repartition)
        print('coalesce ', coalesce)
        print('cache', cache)
        
        # override options on provider with options on resource, with option on the read method
        options = utils.merge(pd.get('read',{}).get('options',{}), md.get('read',{}).get('options',{}))
        options = utils.merge(options, kargs)

        if pd['service'] in ['local', 'hdfs', 'minio']:

            if pd['service'] == 'local':
                root = pd.get('path',project.rootpath())
                root = root if root[0]=='/' else '{}/{}'.format(project.rootpath(), root)
                url = "file://{}/{}".format(root, md['path'])
                url = url.translate(str.maketrans({"{":  r"\{","}":  r"\}"}))
            elif pd['service'] == 'hdfs':
                url = "hdfs://{}:{}/{}/{}".format(pd['hostname'],pd.get('port', '8020'),pd['path'],md['path'])
            elif pd['service'] == 'minio':
                url = "s3a://{}".format(os.path.join(pd['path'],md['path']))
            else:
                print('format unknown')
                return None
            
            print(url)
            if pd['format']=='csv':
                obj= self._ctx.read.csv(url, **options)
            if pd['format']=='json':
                obj= self._ctx.read.option('multiLine',True).json(url, **options)
            if pd['format']=='jsonl':
                obj= self._ctx.read.json(url, **options)
            elif pd['format']=='parquet':
                obj= self._ctx.read.parquet(url, **options)
        
        elif pd['service'] == 'sqlite':
            url = "jdbc:sqlite:" + pd['path']
            driver = "org.sqlite.JDBC"
            obj =  self._ctx.read.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .load(**options)
        elif pd['service'] == 'mysql':
            url = "jdbc:mysql://{}:{}/{}".format(pd['hostname'],pd.get('port', '3306'),pd['database'])
            print(url)
            driver = "com.mysql.jdbc.Driver"
            obj =  self._ctx.read.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .option("user",pd['username']).option('password',pd['password'])\
                   .load(**options)
        elif pd['service'] == 'postgres':
            url = "jdbc:postgresql://{}:{}/{}".format(pd['hostname'],pd.get('port', '5432'),pd['database'])
            print(url)
            driver = "org.postgresql.Driver"
            obj =  self._ctx.read.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .option("user",pd['username']).option('password',pd['password'])\
                   .load(**options)
        elif pd['service'] == 'mssql':
            url = "jdbc:sqlserver://{}:{};databaseName={}".format(pd['hostname'],pd.get('port', '1433'),pd['database'])
            print(url)
            driver = "com.microsoft.sqlserver.jdbc.SQLServerDriver"
            obj = self._ctx.read.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .option("user",pd['username']).option('password',pd['password'])\
                   .load(**options)
        elif pd['service'] == 'elastic':
            uri = 'http://{}:{}/{}'.format(pd["hostname"], pd["port"], md['path'])
            obj = elastic_read(uri=uri, action=md["action"], query=md['query'], format="spark", sparkContext=self._ctx, **kargs)
        else:
            raise('downt know how to handle this')
        
        obj = obj.repartition(repartition) if repartition else obj
        obj = obj.coalesce(coalesce) if coalesce else obj
        obj = obj.cache() if cache else obj

        return obj

    def write(self, obj, resource=None, path=None, provider=None, **kargs):
        md = data.metadata(resource, path, provider)
        if not md:
            print('no valid resource found')
            return

        pd = md['provider']
        
        # override options on provider with options on resource, with option on the read method
        options = utils.merge(pd.get('write',{}).get('options',{}), md.get('write',{}).get('options',{}))
        options = utils.merge(options, kargs)

        cache = pd.get('write',{}).get('cache', False)
        cache = md.get('write',{}).get('cache', cache)
        
        repartition = pd.get('write',{}).get('repartition', None)
        repartition = pd.get('write',{}).get('repartition', repartition)
        
        coalesce = pd.get('write',{}).get('coalesce', None)
        coalesce = md.get('write',{}).get('coalesce', coalesce)

        print('repartition ', repartition)
        print('coalesce ', coalesce)
        print('cache', cache)

        obj = obj.repartition(repartition) if repartition else obj
        obj = obj.coalesce(coalesce) if coalesce else obj
        obj = obj.cache() if cache else obj

        if pd['service'] in ['local', 'hdfs', 'minio']:
            if pd['service'] == 'local':
                root = pd.get('path',project.rootpath())
                root = root if root[0]=='/' else '{}/{}'.format(project.rootpath(), root)
                url = "file://{}/{}".format(root, md['path'])
            elif pd['service'] == 'hdfs':
                url = "hdfs://{}:{}/{}/{}".format(pd['hostname'],pd.get('port', '8020'),pd['path'],md['path'])
            elif pd['service'] == 'minio':
                url = "s3a://{}".format(os.path.join(pd['path'],md['path']))
            print(url)
                        
            if pd['format']=='csv':
                return obj.write.csv(url, **options)
            if pd['format']=='json':
                return obj.write.option('multiLine',True).json(url, **options)
            if pd['format']=='jsonl':
                return obj.write.json(url, **options)
            elif pd['format']=='parquet':
                return obj.write.parquet(url, **options)
            else:
                print('format unknown')
        elif pd['service'] == 'sqlite':
            url = "jdbc:sqlite:" + pd['path']
            driver = "org.sqlite.JDBC"
            return obj.write.format('jdbc').option('url', url)\
                      .option("dbtable", md['path']).option("driver", driver).save(**kargs)
        elif pd['service'] == 'mysql':
            url = "jdbc:mysql://{}:{}/{}".format(pd['hostname'],pd.get('port', '3306'),pd['database'])
            driver = "com.mysql.jdbc.Driver"
            return obj.write.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .option("user",pd['username']).option('password',pd['password'])\
                   .save(**kargs)
        elif pd['service'] == 'postgres':
            url = "jdbc:postgresql://{}:{}/{}".format(pd['hostname'], pd.get('port', '5432'), pd['database'])
            print(url)
            driver = "org.postgresql.Driver"
            return obj.write.format('jdbc').option('url', url)\
                   .option("dbtable", md['path']).option("driver", driver)\
                   .option("user",pd['username']).option('password',pd['password'])\
                   .save(**kargs)
        elif pd['service'] == 'elastic':
            uri = 'http://{}:{}'.format(pd["hostname"], pd["port"])

            if "mode" in kargs and kargs.get("mode") == "overwrite":
                mode = "overwrite"
            else:
                mode = "append"

            elatic_write(obj, uri, mode, md["index"], md["settings"], md["mappings"])
        else:
            raise('downt know how to handle this')


def elastic_read(uri, action, query, format="pandas", sparkContext=None, **kargs):
    """
    :param format: pandas|spark|python
    :param uri:
    :param action:
    :param query:
    :param kargs:
    :return:

    sample resource:
    - variables are enclosed in [[ ]] instead of {{ }}
    keywords_search:
        provider: elastic_test
        #index: search_keywords_dev
        path: /search_keywords_dev/keyword/
        search: _search
        query: >
            {
              "size" : 2,
              "from" : 0,
              "query": {
                "function_score": {
                  "query": {
                      "match" : {
                        "query_wo_tones": {"query":"[[query]]", "operator" :"or", "fuzziness":"AUTO"}
                    }
                  },
                  "script_score" : {
                      "script" : {
                        "source": "Math.sqrt(doc['impressions'].value)"
                      }
                  },
                  "boost_mode":"multiply"
                }
              }
            }
    :param resource:
    :param path:
    :param provider:
    :param kargs: params to replace in `query` template
    :return: pandas dataframe
    """

    es = elasticsearch.Elasticsearch([uri])
    query = query.replace("[[", "{{").replace("]]", "}}")
    # print(query)
    if kargs:
        template = Template(query)
        query = template.render(kargs)
    else:
        pass
    query = json.loads(query)
    print(query)
    if action == "_search":
        res = es.search(body=query)
    elif action == "_msearch":
        res = es.msearch(body=query)
    else:
        raise ("Don't know how to handle this!")

    hits = res.pop("hits", None)
    # return  hits
    if not hits:
        raise("Error")

    res["total_hits"] = hits["total"]
    res["max_score"] = hits["max_score"]

    hits2 = []
    for hit in hits['hits']:
        hitresult = hit.pop("_source", None)
        hits2.append({**hit, **hitresult})

    # return [res, hits2]
    print("Summary:", res)
    # return hits2
    if format == "python":
        return hits2
    elif format == "pandas":
        return pandas.DataFrame(hits2)
    elif format=="spark":
        return sparkContext.createDataFrame(pandas.DataFrame(hits2))
        # return sparkContext.createDataFrame(pandas.DataFrame(hits2))
    else:
        raise ("Unknown format: " + format)


def elatic_write(obj, uri, mode='append', indexName=None, settings=None, mappings=None):
    """
    :param mode: overwrite | append
    :param obj: spark dataframe, pandas dataframe, or list of Python dictionaries
    :param kargs:
    :return:
    """
    es = elasticsearch.Elasticsearch([uri])
    if mode == "overwrite":
        if isinstance(mappings["properties"], str): # original properties JSON in Elastics format
            # properties: >
            #                 {
            #                     "count": {
            #                         "type": "integer"
            #                     },
            #                     "keyword": {
            #                         "type": "keyword"
            #                     },
            #                     "query": {
            #                         "type": "text",
            #                         "fields": {
            #                             "keyword": {
            #                                 "type": "keyword",
            #                                 "ignore_above": 256
            #                             }
            #                         }
            #                     }
            #                 }
            mappings["properties"] = json.loads(mappings["properties"])
        else: # dictionary
            #             properties:
            #                 count: integer
            #                 keyword: keyword
            #                 query:
            #                     type: text
            #                     fields:
            #                         keyword:
            #                             type: keyword
            #                             ignore_above: 256
            #                 query_wo_tones:
            #                     type: text
            #                     fields:
            #                         keyword:
            #                             type: keyword
            #                             ignore_above: 256
            for k, v in mappings["properties"].items():
                if isinstance(mappings["properties"][k], str):
                    mappings["properties"][k] = {"type":mappings["properties"][k]}

        if isinstance(settings, str):  # original settings JSON in Elastics format
            #       settings: >
            #                 {
            #                     "index": {
            #                         "number_of_shards": 1,
            #                         "number_of_replicas": 3,
            #                         "mapping": {
            #                             "total_fields": {
            #                                 "limit": "1024"
            #                             }
            #                         }
            #                     }
            #                 }
            settings = json.loads(settings)
        else: # yaml object parsed into python dictionary
            #         settings:
            #             index:
            #                 number_of_shards: 1
            #                 number_of_replicas: 3
            #                 mapping:
            #                     total_fields:
            #                         limit: 1024
            pass

        print(settings)
        print(mappings["properties"])

        if not settings or not settings:
            raise ("'settings' and 'mappings' are required for 'overwrite' mode!")
        es.indices.delete(index=indexName, ignore=404)
        es.indices.create(index=indexName, body={
            "settings": settings,
            "mappings": {
                mappings["doc_type"]: {
                    "properties": mappings["properties"]
                }
            }
        })
    else: # append
        pass

    import pandas.core.frame
    import pyspark.sql.dataframe
    import numpy as np
    if isinstance(obj, pandas.core.frame.DataFrame):
        obj = obj.replace({np.nan:None}).to_dict(orient='records')
    elif isinstance(obj, pyspark.sql.dataframe.DataFrame):
        obj = obj.toPandas().replace({np.nan:None}).to_dict(orient='records')
    else: # python list of python dictionaries
        pass

    from collections import deque
    deque(elasticsearch.helpers.parallel_bulk(es, obj, index=indexName,
                                              doc_type=mappings["doc_type"]), maxlen=0)
    es.indices.refresh()


def get(name):
    global engines

    #get
    engine = engines.get(name)

    if not engine:
        #create
        md = params.metadata()
        cn = md['engines'].get(name)
        config = cn.get('config', {})

        if cn['context']=='spark':
            engine = SparkEngine(name, config)
            engines[name] = engine

        if cn['context']=='pandas':
            engine = PandasEngine(name, config)
            engines[name] = engine

    return engine
