../../../spark-1.6.0/bin/spark-submit \
 --master local[*] \
--executor-memory 20g  --executor-cores 5  --num-executors 80 \
--jars ../spark-examples_2.10-2.0.0-SNAPSHOT.jar,../random-0.0.1-SNAPSHOT-shaded.jar  \
--py-files ../python-lib.zip \
sha1-infos-escorts-images_hdfs.py  \
$@
