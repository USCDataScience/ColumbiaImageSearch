../../../spark-1.6.0/bin/spark-submit \
 --master local[*] \
--executor-memory 2g  --executor-cores 1  --num-executors 10 \
--jars ../packages/spark-examples_2.10-2.0.0-SNAPSHOT.jar,../packages/random-0.0.1-SNAPSHOT-shaded.jar  \
--py-files ../packages/python-lib.zip \
sha1-infos-escorts-join.py  \
$@
