# v1 has an issue with queueOut.
# Finalizer is not always getting the processed update, seems to hang for a long time. 
# Unclear why, seems to work fine with fake work.
# Workaround in this v2, look for files on disk...

import os
import sys
import time
import datetime
import happybase

# parallel
from multiprocessing import Queue
from multiprocessing import Process

sys.path.append('..')
import cu_image_search
from cu_image_search.search import searcher_hbaseremote

nb_workers = 10

# how much time do we wait before re-trying to finalizer an update if none were found
time_sleep_noupdate = 60 
# queue timeout when trying to get an update to process
queue_timeout = 600

debug_sleep = 10
debug = False

producer_end_signal = (None, None, None)
consumer_end_signal = "consumer_ended"
finalizer_end_signal = "finalizer_ended"

# should we try/except main loop of producer, consumer and finalizer?
def end_producer(queueIn):
    print "[producer-pid({}): log] ending producer at {}".format(os.getpid(), get_now())
    for i in range(nb_workers):
        # sentinel value, one for each worker
        queueIn.put(producer_end_signal)


def producer(global_conf_file, queueIn, queueProducer):
    print "[producer-pid({}): log] Started a producer worker at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    searcher_producer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[producer-pid({}): log] Producer worker ready at {}".format(os.getpid(), get_now())
    queueProducer.put("Producer ready")
    while True:
        try:
            start_get_batch = time.time()
            update_id, str_list_sha1s = searcher_producer.indexer.get_next_batch_precomp_sim()
            #queueProducer.put("Producer got batch")
            print "[producer-pid({}): log] Got batch in {}s at {}".format(os.getpid(), time.time() - start_get_batch, get_now())
            sys.stdout.flush()
            if update_id is None:
                print "[producer-pid({}): log] No more update to process.".format(os.getpid())
                return end_producer(queueIn)
            else:
                start_precomp = time.time()
                # check that sha1s of batch have no precomputed similarities already in sha1_infos table
                valid_sha1s, not_indexed_sha1s, precomp_sim_sha1s = check_indexed_noprecomp(searcher_producer, str_list_sha1s.split(','))
                # should we split valid_sha1s in batches of 100 or something smaller than 10K currently?
                searcher_producer.indexer.write_batch([(update_id, {searcher_producer.indexer.precomp_start_marker: 'True'})], searcher_producer.indexer.table_updateinfos_name)
                # push updates to be processed in queueIn
                # https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Queue.qsize
                # qsize raises NotImplemented Error on OS X...
                #print "[producer: log] Pushing update {} in queue containing {} items at {}.".format(update_id, queueIn.qsize(), get_now())
                print "[producer-pid({}): log] Pushing update {} at {}.".format(os.getpid(), update_id, get_now())
                sys.stdout.flush()
                queueIn.put((update_id, valid_sha1s, start_precomp))
                print "[producer-pid({}): log] Pushed update {} to queueIn at {}.".format(os.getpid(), update_id, get_now())
                sys.stdout.flush()
        except Exception as inst:
            print "[producer-pid({}): error] Error at {}. Leaving. Error was: {}".format(os.getpid(), get_now(), inst)
            return end_producer(queueIn)


def end_consumer(queueIn, queueOut):
    print "[consumer-pid({}): log] ending consumer at {}".format(os.getpid(), get_now())
    queueOut.put(consumer_end_signal)
            


def consumer(global_conf_file, queueIn, queueOut, queueConsumer):
    print "[consumer-pid({}): log] Started a consumer worker at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    searcher_consumer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[consumer-pid({}): log] Consumer worker ready at {}".format(os.getpid(), get_now())
    queueConsumer.put("Consumer ready")
    sys.stdout.flush()
    while True:
        try:
            ## reads from queueIn
            print "[consumer-pid({}): log] Consumer worker waiting for update at {}".format(os.getpid(), get_now())
            sys.stdout.flush()
            update_id, valid_sha1s, start_precomp = queueIn.get(True, queue_timeout)
            if update_id is None:
                # declare worker ended
                print "[consumer-pid({}): log] Consumer worker ending at {}".format(os.getpid(), get_now())
                return end_consumer(queueIn, queueOut)
            ## search
            print "[consumer-pid({}): log] Consumer worker computing similarities for {} valid sha1s of update {} at {}".format(os.getpid(), len(valid_sha1s), update_id, get_now())
            sys.stdout.flush()
            start_search = time.time()
            # precompute similarities using searcher 
            simname, corrupted = searcher_consumer.search_from_listid_get_simname(valid_sha1s, update_id, check_already_computed=True)
            elapsed_search = time.time() - start_search
            print "[consumer-pid({}): log] Consumer worker processed update {} at {}. Search performed in {}s.".format(os.getpid(), update_id, get_now(), elapsed_search)
            sys.stdout.flush()
        except Exception as inst:
            print "[consumer-pid({}): error] Consumer worker caught error at {}. Error was {}".format(os.getpid(), get_now(), inst)


def end_finalizer(queueFinalizer):
    print "[finalizer-pid({}): log] ending finalizer at {}".format(os.getpid(), get_now())
    queueFinalizer.put("Finalizer ended")


def finalize_udpate_list(list_simfiles, searcher_finalizer, partial=True):
    found_update = False
    for simname in list_simfiles:
        # parse update_id
        found_update = True
        start_finalize = time.time()
        update_id = simname.split('-')[0]
        print "[finalizer-pid({}): log] Finalizer worker found update {} to finalize at {}".format(os.getpid(), update_id, get_now())
        sys.stdout.flush()

        ## Push computed similarities
        
        # format for saving in HBase:
        # - batch_sim: should be a list of sha1 row key, dict of "s:similar_sha1": dist_value
        # - batch_mark_precomp_sim: should be a list of sha1 row key, dict of precomp_sim_column: True
        batch_sim, batch_mark_precomp_sim = format_batch_sim_v2(simname, searcher_finalizer)

        # push similarities to HBI_table_sim (escorts_images_similar_row_dev) using searcher.indexer.write_batch
        if batch_sim:
            searcher_finalizer.indexer.write_batch(batch_sim, searcher_finalizer.indexer.table_sim_name)
            # push to weekly update table for Amandeep to integrate in DIG
            week, year = get_week_year()
            weekly_sim_table_name = searcher_finalizer.indexer.table_sim_name+"_Y{}W{}".format(year, week)
            print "[finalizer-pid({}): log] weekly table name: {}".format(os.getpid(), weekly_sim_table_name)
            weekly_sim_table = searcher_finalizer.indexer.get_create_table(weekly_sim_table_name, families={'s': dict()})
            searcher_finalizer.indexer.write_batch(batch_sim, weekly_sim_table_name)

            ## Mark as done
            # mark precomp_sim true in escorts_images_sha1_infos
            searcher_finalizer.indexer.write_batch(batch_mark_precomp_sim, searcher_finalizer.indexer.table_sha1infos_name)
            # mark update has processed 
            if not partial:
                searcher_finalizer.indexer.write_batch([(update_id, {searcher_finalizer.indexer.precomp_end_marker: 'True'})],
                                               searcher_finalizer.indexer.table_updateinfos_name)
        
        ## Cleanup
        try:
            # remove simname 
            os.remove(simname)
            # remove features file
            featfn = update_id+'.dat'
            os.remove(featfn)
        except Exception as inst:
            print "[finalizer-pid({}): error] Could not cleanup. Error was: {}".format(os.getpid(), inst)

        print "[finalizer-pid({}): log] Finalized update {} at {} in {}s.".format(os.getpid(), update_id, get_now(), time.time() - start_finalize)
        sys.stdout.flush()
    return found_update
    

def finalizer(global_conf_file, queueOut, queueFinalizer):
    print "[finalizer-pid({}): log] Started a finalizer worker at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    import glob
    searcher_finalizer = searcher_hbaseremote.Searcher(global_conf_file)
    print "[finalizer-pid({}): log] Finalizer worker ready at {}".format(os.getpid(), get_now())
    sys.stdout.flush()
    queueFinalizer.put("Finalizer ready")
    count_workers_ended = 0
    sim_pattern = '*-sim_'+str(searcher_finalizer.ratio)+'.txt'
    sim_partial_pattern = '*-sim_partial.txt'
    while True:
        try:
            print "[finalizer-pid({}): log] Finalizer worker waiting for an update at {}".format(os.getpid(), get_now())
            sys.stdout.flush()
            found_update = False

            ## Use glob to list of files that would match the simname pattern.
            list_simfiles = glob.glob(sim_pattern)
            found_update = finalize_udpate_list(list_simfiles, searcher_finalizer, partial=False)

            ## Push previously computed similarities for batches that did not complete
            list_simfiles = glob.glob(sim_partial_pattern)
            found_update_partial = finalize_udpate_list(list_simfiles, searcher_finalizer, partial=True)
            
            # Check if consumers have ended
            try:
                end_signal = queueOut.get(block=False)
                if end_signal == consumer_end_signal:
                    count_workers_ended += 1
                    print "[finalizer-pid({}): log] {} consumer workers ended out of {} at {}.".format(os.getpid(), count_workers_ended, nb_workers, get_now())
                    if count_workers_ended == nb_workers:
                        # should we check for intermediate sim patterns to know if consumers are actually still running, or failed?
                        # sim_pattern = '*-sim.txt'
                        # fully done
                        print "[finalizer-pid({}): log] All consumer workers ended at {}. Leaving.".format(os.getpid(), get_now())
                        return end_finalizer(queueFinalizer)
                    continue
            except Exception as inst: #timeout
                pass

            # Sleep if no updates where found in this loop cycle?
            if not found_update:
                time.sleep(time_sleep_noupdate)

        except Exception as inst:
            #[finalizer: error] Caught error at 2017-04-14:04.29.23. Leaving. Error was: list index out of range
            print "[finalizer-pid({}): error] Caught error at {}. Error {} was: {}".format(os.getpid(), get_now(), type(inst), inst)
            # now we catch timeout too, so we are no longer leaving...
            #return end_finalizer(queueOut, queueFinalizer)


def get_now():
    return datetime.datetime.now().strftime("%Y-%m-%d:%H.%M.%S")


def get_week(today=datetime.datetime.now()):
    return today.strftime("%W")


def get_year(today=datetime.datetime.now()):
    return today.strftime("%Y")


def get_week_year(today=datetime.datetime.now()):
    week = get_week(today)
    year = get_year(today)
    return week, year


def check_indexed_noprecomp(searcher, list_sha1s):
    print "[check_indexed_noprecomp: log] verifying validity of list_sha1s."
    sys.stdout.flush()
    columns_check = [searcher.indexer.cu_feat_id_column, searcher.indexer.precomp_sim_column]
    # Is this blocking in parallel mode?
    rows = searcher.indexer.get_columns_from_sha1_rows(list_sha1s, columns=columns_check)
    not_indexed_sha1s = []
    precomp_sim_sha1s = []
    valid_sha1s = []
    for row in rows:
        #print row
        # check searcher.indexer.cu_feat_id_column exists
        if searcher.indexer.cu_feat_id_column not in row[1]:
            not_indexed_sha1s.append(str(row[0]))
            print "[check_indexed_noprecomp: log] found unindexed image {}".format(str(row[0]))
            sys.stdout.flush()
            continue
        # check searcher.indexer.precomp_sim_column does not exist
        if searcher.indexer.precomp_sim_column in row[1]:
            precomp_sim_sha1s.append(str(row[0]))
            #print "[check_indexed_noprecomp: log] found image {} with already precomputed similar images".format(str(row[0]))
            #sys.stdout.flush()
            continue
        valid_sha1s.append((long(row[1][searcher.indexer.cu_feat_id_column]), str(row[0])))
    # v1 was:
    #valid_sha1s = list(set(list_sha1s) - set(not_indexed_sha1s) - set(precomp_sim_sha1s))
    msg = "{} valid sha1s, {} not indexed sha1s, {} already precomputed similarities sha1s."
    print("[check_indexed_noprecomp: log] "+msg.format(len(valid_sha1s), len(not_indexed_sha1s), len(precomp_sim_sha1s)))
    sys.stdout.flush()
    return valid_sha1s, not_indexed_sha1s, precomp_sim_sha1s


def read_sim_precomp_v2(simname, searcher, nb_query=None):
    # intialization
    sim = []
    sim_score = []
    if simname is not None:
        # read similar images
        count = 0
        f = open(simname);
        for line in f:
            #sim_index.append([])
            nums = line.replace(' \n','').split(' ')
            #filter near duplicate here
            nums = searcher.filter_near_dup(nums, searcher.near_dup_th)
            #print nums
            onum = len(nums)/2
            n = onum
            #print n
            # this is not really possible since we are querying with DB images here.
            if onum==0: # no returned images, e.g. no near duplicate
                sim.append(())
                sim_score.append([])
                continue
            # get the sha1s of similar images
            sim_infos = [searcher.indexer.sha1_featid_mapping[int(i)] for i in nums[0:n]]
            # beware, need to make sure sim and sim_score are still aligned
            #print("[read_sim] got {} sim_infos from {} samples".format(len(sim_infos), n))
            sim.append(sim_infos)
            sim_score.append(nums[onum:onum+n])
            count = count + 1
            if nb_query and count == nb_query:
                break
        f.close()
    return sim, sim_score
    

def format_batch_sim_v2(simname, searcher):
    # format similarities for HBase output
    sim, sim_score = read_sim_precomp_v2(simname, searcher)
    print "[format_batch_sim_v2: log] {} has {} images with precomputed similarities.".format(simname, len(sim))
    sys.stdout.flush()
    # batch_sim: should be a list of sha1 row key, dict of all "s:similar_sha1": dist_value
    batch_sim = []
    # batch_mark_precomp_sim: should be a list of sha1 row key, dict of precomp_sim_column: True
    batch_mark_precomp_sim = []
    if sim:
        for i_img,list_sim in enumerate(sim):
            # query sha1
            sha1 = list_sim[0]
            list_score = sim_score[i_img]
            
            sim_columns = dict()
            for i_sim,sim_img in enumerate(list_sim):
                sim_columns["s:"+str(sim_img)] = str(list_score[i_sim])
                # to store similar image -> query
                sim_reverse = dict()
                sim_reverse["s:"+sha1] = str(list_score[i_sim])
                batch_sim.append((str(sim_img), sim_reverse))
            sim_row = (sha1, sim_columns)
            batch_sim.append(sim_row)
            batch_mark_precomp_sim.append((sha1,{searcher.indexer.precomp_sim_column: 'True'}))
    
    return batch_sim, batch_mark_precomp_sim


def parallel_precompute(global_conf_file):
    # Define queues
    queueIn = Queue(nb_workers+2)
    # Only to signal end now
    queueOut = Queue(nb_workers)
    queueProducer = Queue()
    queueFinalizer = Queue()
    queueConsumer = Queue(nb_workers)

    # Start finalizer
    t = Process(target=finalizer, args=(global_conf_file, queueOut, queueFinalizer))
    t.daemon = True
    t.start()
    # Start consumers
    for i in range(nb_workers):
        t = Process(target=consumer, args=(global_conf_file, queueIn, queueOut, queueConsumer))
        t.daemon = True
        t.start()
    # Start producer
    t = Process(target=producer, args=(global_conf_file, queueIn, queueProducer))
    t.daemon = True
    t.start()

    # Wait for everything to be started properly
    producerOK = queueProducer.get()
    #queueProducer.task_done()
    finalizerOK = queueFinalizer.get()
    #queueFinalizer.task_done()
    for i in range(nb_workers):
        consumerOK = queueConsumer.get()
        #queueConsumer.task_done()
    print "[parallel_precompute: log] All workers are ready."
    sys.stdout.flush()
    # Wait for everything to be finished
    finalizerEnded = queueFinalizer.get()
    print finalizerEnded
    return
    


if __name__ == "__main__":
    
    """ Run precompute similar images based on `conf_file` given as parameter
    """
    if len(sys.argv)<2:
        print "python precompute_similar_images_parallel_v2.py conf_file"
        exit(-1)
    global_conf_file = sys.argv[1]
    
    while True:
        parallel_precompute(global_conf_file)
        print "[precompute_similar_images_parallel: log] Nothing to compute. Sleeping for {}s.".format(time_sleep)
        sys.stdout.flush()
        time.sleep(time_sleep)
    
    
    
