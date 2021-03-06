#!/bin/python
#
# /home/01131/tg804093/work/spark-2.2.0-bin-hadoop2.7/bin/spark-submit --packages org.apache.spark:spark-streaming-kafka-0-8_2.11:2.0.2 --conf "spark.executor.memory=110g" --files ../saga_hadoop_utils.py KMeans_SparkStreamingThroughputConsumer.py 
# 
# With app log:
#  /home/01131/tg804093/work/spark-2.2.0-bin-hadoop2.7/bin/spark-submit --packages org.apache.spark:spark-streaming-kafka-0-8_2.11:2.0.2 --conf spark.eventLog.enabled=true --conf "spark.eventLog.dir=file:///work/01131/tg804093/wrangler/spark-logs" --files ../saga_hadoop_utils.py  KMeans_SparkStreamingThroughputConsumer.py


import os
import sys
sys.path.append("/home/01131/tg804093/notebooks/Pilot-Memory/examples/jupyter/util")
sys.path.append("/home/01131/tg804093/notebooks/Pilot-Memory/examples/jupyter/streaming")

import pickle
import time
import datetime
start = time.time()
import logging
logging.basicConfig(level=logging.WARN)
import init_spark_wrangler
from pilot_hadoop import PilotComputeService as PilotSparkComputeService
from pyspark.mllib.linalg import Vectors
from pyspark.mllib.regression import LabeledPoint
from pyspark.mllib.clustering import StreamingKMeans
from pyspark.streaming import StreamingContext
from pyspark.streaming.kafka import KafkaUtils, TopicAndPartition
import numpy as np
import msgpack
import msgpack_numpy as m
import urllib, json
import socket
import saga_hadoop_utils
import re
import pandas as pd
from subprocess import check_output
from pyspark.streaming.listener import StreamingListener




#######################################################################################
# CONFIGURATIONS
# Get current cluster setup from work directory
master_host=saga_hadoop_utils.get_spark_master(os.path.expanduser('~'))
kafka_details = saga_hadoop_utils.get_kafka_config_details(os.path.expanduser('~'))
print kafka_details                                       
SPARK_MASTER="spark://" + master_host +":7077"
SPARK_APP_PORT=4040 #4041 if ngrok is running
#SARK_MASTER="local[1]"
SPARK_LOCAL_IP=socket.gethostbyname(socket.gethostname())
KAFKA_ZK=kafka_details[1]
METABROKER_LIST=",".join(kafka_details[0])
TOPIC='Throughput'
NUMBER_EXECUTORS=1
STREAMING_WINDOW=10
SCENARIO="16_Producer_Kmeans"
#SCENARIO="1_Producer_Count"
#######################################################################################


def get_number_partitions():
    cmd = "/home/01131/tg804093/work/kafka_2.11-0.10.1.0/bin/kafka-topics.sh --describe --topic %s --zookeeper %s"%(TOPIC, KAFKA_ZK)
    print cmd
    out = check_output(cmd, shell=True)
    number=re.search("(?<=PartitionCount:)[0-9]*", out).group(0)
    return number


NUMBER_PARTITIONS = get_number_partitions()
print "Number Partitions: "   + NUMBER_PARTITIONS

print "Spark Master: " + SPARK_MASTER

fromOffset = {}
for i in range(int(NUMBER_PARTITIONS)):
    topicPartion = TopicAndPartition(TOPIC, i)
    fromOffset = {topicPartion: long(0)}

#######################################################################################
# Logging
run_timestamp=datetime.datetime.now()
RESULT_FILE= "results/spark-" + run_timestamp.strftime("%Y%m%d-%H%M%S") + ".csv"
SPARK_RESULT_FILE="results/spark-metrics-" + run_timestamp.strftime("%Y%m%d-%H%M%S") + ".csv"

try:
    os.makedirs("results")
except:
    pass

output_file=open(RESULT_FILE, "w")
output_spark_metrics=open(SPARK_RESULT_FILE, "w")
output_spark_metrics.write("BatchTime, SubmissionTime, SchedulingDelay, TotalDelay, NumberRecords, Scenario\n")
       

#######################################################################################
# Spark Helper
    
os.environ["SPARK_LOCAL_IP"]=SPARK_LOCAL_IP

pilotcompute_description = {
    "service_url": SPARK_MASTER,
    "spark.driver.host": os.environ["SPARK_LOCAL_IP"]
}


print "SPARK HOME: %s"%os.environ["SPARK_HOME"]
print "PYTHONPATH: %s"%os.environ["PYTHONPATH"]

def get_application_details(sc):
    app_id=sc.applicationId
    url = "http://" + SPARK_LOCAL_IP + ":" + str(SPARK_APP_PORT)+ "/api/v1/applications/"+ app_id + "/executors"
    max_id = -1
    while True:
        cores = 0
        response = urllib.urlopen(url)
        data = json.loads(response.read())
        print data
        for i in data:
            print "Process %s"%i["id"]
            if i["id"]!="driver":
                cores = cores + i["totalCores"]
                if int(i["id"]) > max_id: max_id = int(i["id"])
        print "Max_id: %d, Number Executors: %d"%(max_id, NUMBER_EXECUTORS)
        if (max_id == (NUMBER_EXECUTORS-1)):
            break
        time.sleep(.1)
            
            
    # http://129.114.58.102:4040/api/v1/applications/
    # http://129.114.58.102:4040/api/v1/applications/app-20160821102227-0003/executors
    return cores
    #return 1
    
    
def get_streaming_performance_details(app_id, filename):
    """curl http://localhost:4041/api/v1/applications/app-20170805185159-0004/streaming/batches"""
    url = "http://" + SPARK_LOCAL_IP + ":" + str(SPARK_APP_PORT)+ "/api/v1/applications/"+ app_id + "/streaming/batches"
    response = urllib.urlopen(url)
    df=pd.read_json(response.read())
    df.to_csv(filename)

    
#######################################################################################
# Collecting Performance Information about batch throughput
class BatchInfoCollector(StreamingListener):

    def __init__(self):
        super(StreamingListener, self).__init__()
        self.batchInfosCompleted = []
        self.batchInfosStarted = []
        self.batchInfosSubmitted = []

    def onBatchSubmitted(self, batchSubmitted):
        self.batchInfosSubmitted.append(batchSubmitted.batchInfo())

    def onBatchStarted(self, batchStarted):
        self.batchInfosStarted.append(batchStarted.batchInfo())

    def onBatchCompleted(self, batchCompleted):
        info = batchCompleted.batchInfo()
        submissionTime = datetime.datetime.fromtimestamp(info.submissionTime()/1000).isoformat()
        
        output_spark_metrics.write("%s, %s, %d, %d, %d,%s\n"%(str(info.batchTime()), submissionTime, \
                                                         info.schedulingDelay(), info.totalDelay(), info.numRecords(), SCENARIO))
        output_spark_metrics.flush()
        self.batchInfosCompleted.append(batchCompleted.batchInfo())

    def onStreamingStarted(self, streamStarted):  
        pass
        
    def onReceiverStarted(self, receiverStarted):
        """
        Called when a receiver has been started
        """
        pass

    def onReceiverError(self, receiverError):
        """
        Called when a receiver has reported an error
        """
        pass

    def onReceiverStopped(self, receiverStopped):
        """
        Called when a receiver has been stopped
        """
        pass


    def onOutputOperationStarted(self, outputOperationStarted):
        """
        Called when processing of a job of a batch has started.
        """
        pass

    def onOutputOperationCompleted(self, outputOperationCompleted):
        """
        Called when processing of a job of a batch has completed
        """
        pass        
            
#######################################################################################
# Init Spark
            
start = time.time()
pilot_spark = PilotSparkComputeService.create_pilot(pilotcompute_description=pilotcompute_description)
sc = pilot_spark.get_spark_context()
spark_cores=get_application_details(sc)
#print str(sc.parallelize([2,3]).collect())
output_file.write("Measurement,Spark Cores,Number Points,Number_Partitions, Time\n")
output_file.write("Spark Startup, %d, %d, %s, %.5f\n"%(spark_cores, -1, NUMBER_PARTITIONS, time.time()-start))
output_file.flush()

#######################################################################################
# KMeans Functions
decayFactor=1.0
timeUnit="batches"
global max_value 
max_value = 0

model = StreamingKMeans(k=10, decayFactor=decayFactor, timeUnit=timeUnit).setRandomCenters(3, 1.0, 0)

def printOffsetRanges(rdd):
    for o in offsetRanges:
        print "%s %s %s %s" % (o.topic, o.partition, o.fromOffset, o.untilOffset)

def count_records(rdd):    
    count = rdd.count()
    return count
        
def pre_process(datetime, rdd):  
    #print (str(type(time)) + " " + str(type(rdd)))    
    start = time.time()    
    points=rdd.map(lambda p: p[1]).flatMap(lambda a: eval(a)).map(lambda a: Vectors.dense(a))
    end_preproc=time.time()
    count = points.count()
    end_count = time.time()
    output_file.write("Points PreProcess, %d, %d, %s, %.5f\n"%(spark_cores, count, NUMBER_PARTITIONS, end_preproc-start))
    output_file.write("Points Count, %d, %d, %s, %.5f\n"%(spark_cores, count, NUMBER_PARTITIONS, end_count-end_preproc))
    output_file.flush()
    return points
    #points.pprint()
    #model.trainOn(points)

def model_update(rdd):
    count = rdd.count()
    start = time.time()
    lastest_model = model.latestModel()
    lastest_model.update(rdd, decayFactor, timeUnit)    
    end_train = time.time()
    #predictions=model.predictOn(points)
    #end_pred = time.time()    
    output_file.write("KMeans Model Update, %d, %d, %s, %.5f\n"%(spark_cores, count,NUMBER_PARTITIONS, end_train-start))
    output_file.flush()
    #output_file.write("KMeans Prediction, %.3f\n"%(end_pred-end_train))
    #return predictions
    

def model_prediction(rdd):
    pass


##########################################################################################################################
# Start Streaming App
    
ssc_start = time.time()    
ssc = StreamingContext(sc, STREAMING_WINDOW)

batch_collector = BatchInfoCollector()
ssc.addStreamingListener(batch_collector)
      

#kafka_dstream = KafkaUtils.createStream(ssc, KAFKA_ZK, "spark-streaming-consumer", {TOPIC: 1})
#kafka_param: "metadata.broker.list": brokers
#              "auto.offset.reset" : "smallest" # start from beginning
kafka_dstream = KafkaUtils.createDirectStream(ssc, [TOPIC], {"metadata.broker.list": METABROKER_LIST,
                                                             "auto.offset.reset" : "smallest"}) #, fromOffsets=fromOffset)
ssc_end = time.time()    
output_file.write("Spark SSC Startup, %d, %d, %s, %.5f\n"%(spark_cores, -1, NUMBER_PARTITIONS, ssc_end-ssc_start))


#####################################################################
# Scenario Count

#global counts
#counts=[]
#kafka_dstream.foreachRDD(lambda t, rdd: counts.append(rdd.count()))



#####################################################################
# Scenario KMeans

points = kafka_dstream.transform(pre_process)
#points.pprint()
points.foreachRDD(model_update)





#####################################################################
# Other

#points = kafka_dstream.transform(pre_process)
#points.count().pprint()
#print "********Found %d *************"%sum(counts)

#global count_messages 
#count_messages  = sum(counts)
#
#output_file.write(str(counts))
#kafka_dstream.count().pprint()

#print str(counts)
#count = kafka_dstream.count().reduce(lambda a, b: a+b).foreachRDD(lambda a: a.count())
#if count==None:
#    count=0
#print "Number of Records: %d"%count




#predictions=model_update(points)
#predictions.pprint()



#We create a model with random clusters and specify the number of clusters to find
#model = StreamingKMeans(k=10, decayFactor=1.0).setRandomCenters(3, 1.0, 0)
#points=kafka_dstream.map(lambda p: p[1]).flatMap(lambda a: eval(a)).map(lambda a: Vectors.dense(a))
#points.pprint()


# Now register the streams for training and testing and start the job,
# printing the predicted cluster assignments on new data points as they arrive.
#model.trainOn(trainingStream)
#result = model.predictOnValues(testingStream.map(lambda point: ))
#result.pprint()

# Word Count
#lines = kvs.map(lambda x: x[1])
#counts = lines.flatMap(lambda line: line.split(" ")) \
#        .map(lambda word: (word, 1)) \
#        .reduceByKey(lambda a, b: a+b)
#counts.pprint()

ssc.start()
ssc.awaitTermination()
ssc.stop(stopSparkContext=True, stopGraceFully=True)
output_file.close()
output_spark_metrics.close()
