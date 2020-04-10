from flask import Flask, request
import json
import sys
import scipy.stats as stats
from networkx import DiGraph
from networkx.algorithms import is_directed_acyclic_graph
from networkx.algorithms.traversal.edgedfs import edge_dfs
from mysql.connector import Error, connect
import urllib.request

api = Flask(__name__)
connection = {}

EVENT_PROB_THRESHOLD=0.25
TASK_PERCENTILE_THRESHOLD_LOW=5.0
TASK_PERCENTILE_THRESHOLD_HIGH=95.0
LABEL_BLACKLIST = ["ThreadLocalBaggage::Branch", "ThreadLocalBaggage::Set", "ThreadLocalBaggage::Swap", "ThreadLocalBaggage::Join", "ThreadLocalBaggage::Delete"]

def get_overview(trace_id):
    global connection
    cursor = connection.cursor()
    query = ("SELECT overview.duration, overview.doc, tags.tag FROM overview, tags WHERE overview.trace_id=%s AND tags.trace_id=%s")
    cursor.execute(query, (trace_id, trace_id))
    overview = {}
    tags = []
    trace_dur = 0
    trace_doc = ""
    for (duration, doc, tag) in cursor:
        tags += [tag]
        trace_dur = duration
        trace_doc = doc
    overview["tags"] = tags
    overview["duration"] = trace_dur
    overview["timestamp"] = trace_doc
    cursor.close()
    return overview

def get_events(trace_id):
    events = []
    with urllib.request.urlopen("http://198.162.52.119:9000/traces/" + trace_id) as url:
        events = json.loads(url.read().decode())
    return events

def get_tasks(trace_id):
    tasks = []
    with urllib.request.urlopen("http://198.162.52.119:9000/tasks/" + trace_id) as url:
        tasks = json.loads(url.read().decode())
    return tasks

def generate_trace_summary(events, tasks, overview):
    summary = "Done boss!"

    percentile_scores = {}
    concurrent_tasks = {}
    task_name = {}
    task_events = {}
    num_tasks_percentile_high = 0
    num_tasks_percentile_low = 0
    max_task_contention = 0
    max_task_contention_id = ""
    for task in tasks:
        task_id = task['ProcessName'] + str(task['ProcessID']) + str(task['ThreadID'])
        task_name[task_id] = task['Operation']
        # For each task calculate it's percentile score w.r.t to the distribution
        percentile = stats.percentileofscore(task['Data'], task['Duration'])
        percentile_scores[task_id] = percentile
        if percentile > TASK_PERCENTILE_THRESHOLD_HIGH:
            num_tasks_percentile_high += 1
        if percentile < TASK_PERCENTILE_THRESHOLD_LOW:
            num_tasks_percentile_low += 1
        # For each task figure out how many other consequent tasks are being run
        # Approximate this by figuring out how many concurrent tasks there were during
        # the full duration of the task. 
        num_tasks = len(task['MolehillData'])
        if num_tasks > max_task_contention:
            max_task_contention = num_tasks
            max_task_contention_id = task_id
        concurrent_tasks[task_id] = num_tasks
        # Initialize the events array for each task
        task_events[task_id] = []

    eventInfo = {}
    eventGraph = DiGraph()
    anomalous_events_set = set()
    # Process events, find anomalous events, and group events by task 
    for event in events:
        eventInfo[event["EventID"]] = event
        eventGraph.add_node(event["EventID"])
        if(event["ParentEventID"] != None):
            for parentID in event["ParentEventID"]:
                eventGraph.add_edge(parentID, event["EventID"])
        # Get all the anomalous events
        if event["Probability"] < EVENT_PROB_THRESHOLD:
            anomalous_events_set.add(event['EventID'])
        # Put the events in each task
        task_id = event['ProcessName'] + str(event['ProcessID']) + str(event['ThreadID'])
        task_events[task_id] += [event['EventID']]

    # Based on the above data, generate a templated summary for the trace
    template_summary = ""
    # Overview of the trace
    template_summary += "The trace was created on " + str(overview["timestamp"].strftime("%d %b, %Y")) + " and it took around " + str(int(overview["duration"]/1e6)) + " seconds to complete."
    template_summary += "The trace was associated with the following tags: " + ''.join(overview["tags"]) + ". "
    # Number of anomalous events. 
    template_summary += "The trace had " + str(len(events)) + " events, out of which " + str(len(anomalous_events_set)) + " events had less than " + str(EVENT_PROB_THRESHOLD * 100) + "% chance of occurring."
    # Tasks Overview
    template_summary += " The execution of the request was performed by " + str(len(task_name)) + " tasks." 
    if num_tasks_percentile_high > 0:
        template_summary += " " + str(num_tasks_percentile_high) + " tasks had latency that ranked higher than the " + str(int(TASK_PERCENTILE_THRESHOLD_HIGH)) + "th percentile of the latency distribution for the respective task."
    if num_tasks_percentile_low < 0:
        template_summary += " " + str(num_tasks_percentile_low) + " tasks had latency that ranked lower than the " + str(int(TASK_PERCENTILE_THRESHOLD_LOW)) + "th percentile of the latency distribution for the respective task."
    # Contention Overview
    if max_task_contention > 0:
        template_summary += " Task performing the operation " + task_name[max_task_contention_id] + " had the maximum amount of contention with " + str(max_task_contention) + " other tasks performing the same operation at the same time for different requests." 

    # join labels for each task to get summary for each task
    task_summaries = {}
    task_start_times = {}
    for task, events in task_events.items():
        # Sort the events based on timestamp
        # Then join the event labels for a summary of that task
        events_array = [eventInfo[event] for event in events]
        sorted_events = sorted(events_array, key=lambda k : k['HRT'])
        task_summary = ''
        
        # Grab the first event in the task and check its parent to find the task that created 
        # the current one, if it has one. If so add to the summary.
        if(sorted_events[0]["ParentEventID"] is not None):
            parent_task_id = eventInfo[sorted_events[0]["ParentEventID"][0]]['ProcessName']\
                + str(eventInfo[sorted_events[0]["ParentEventID"][0]]['ProcessID'])\
                + str(eventInfo[sorted_events[0]["ParentEventID"][0]]['ThreadID'])
            task_summary += "Task " + task_name[parent_task_id] + " created task " + task_name[task] + ". "
            

        # Only include events that are user-annotated and not anything from the library.
        labels = [event['Label'] if event['Label'] not in LABEL_BLACKLIST else '' for event in sorted_events]
        
        # Append non empty labels to the summary. 
        for l in labels:
            if l != '':
                task_summary += l + ". "
        task_summaries[task] = task_summary
        task_start_times[task] = sorted_events[0]['HRT']

    # Join the individual summaries of each task to get
    # the execution summary of the trace
    execution_summary = ""
    for task, summary in sorted(task_summaries.items(), key=lambda k : task_start_times[k[0]]):
        execution_summary += task_name[task] + " - " + summary + "\n" 


    summary = template_summary + "\n\n" + execution_summary

    return summary

@api.route("/summary/<string:trace_id>", methods=['GET'])
def summary(trace_id):
    print("Generating summary for", trace_id)
    overview = get_overview(trace_id)
    # Get list of events for this trace
    events = get_events(trace_id)
    # Get list of tasks for this trace
    tasks = get_tasks(trace_id)
    summary = generate_trace_summary(events, tasks, overview)
    return summary

def main():
    global connection
    if len(sys.argv) != 2:
        print("Usage: python tracey.py <config_file>")
        sys.exit(1)
    config_file = sys.argv[1]
    with open(config_file, 'r+') as inf:
        config = json.load(inf)
        db_host = config["db_host"]
        database = config["db"]
        user = config["username"]
        password = config["pwd"]
        try:
            connection = connect(host=db_host, database=database, user=user, password=password)
            if connection.is_connected():
                print("Connected to MySQL server")
        except Error as e:
            print("Error while connecting to database", e)
            sys.exit(1)
    api.run(host=config["host"], port=config["port"])

if __name__ == '__main__':
    main()
