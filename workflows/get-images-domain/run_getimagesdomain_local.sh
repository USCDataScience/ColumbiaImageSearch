../../../spark-1.6.0/bin/spark-submit \
 --master local[4] \
--executor-memory 2g  --executor-cores 2  --num-executors 2 \
--jars ../packages/spark-examples_2.10-2.0.0-SNAPSHOT.jar,../packages/elasticsearch-hadoop-2.3.2.jar,../packages/random-0.0.1-SNAPSHOT-shaded.jar  \
--py-files ../packages/python-lib.zip,image_dl.py \
get-images-domain.py   \
$@

