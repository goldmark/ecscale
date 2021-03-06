#!/bin/python
import boto3
import datetime
from optparse import OptionParser
import os

SCALE_IN_CPU_TH = 30
SCALE_IN_MEM_TH = 60
FUTURE_MEM_TH = 70
ECS_AVOID_STR = 'awseb'


def clusters(ecsClient):
    # Returns an iterable list of cluster names
    response = ecsClient.list_clusters()
    if not response['clusterArns']:
        print 'No ECS cluster found'
        exit

    return [cluster for cluster in response['clusterArns'] if ECS_AVOID_STR not in cluster]


def cluster_memory_reservation(cwClient, clusterName):
    # Return cluster mem reservation average per minute cloudwatch metric
    try:
        response = cwClient.get_metric_statistics( 
            Namespace='AWS/ECS',
            MetricName='MemoryReservation',
            Dimensions=[
                {
                    'Name': 'ClusterName',
                    'Value': clusterName
                },
            ],
            StartTime=datetime.datetime.utcnow() - datetime.timedelta(seconds=120),
            EndTime=datetime.datetime.utcnow(),
            Period=60,
            Statistics=['Average']
        )
        return response['Datapoints'][0]['Average']

    except Exception:
        print 'Could not retrieve mem reservation for {}'.format(clusterName)


def find_asg(clusterName, asgData):
    # Returns auto scaling group resourceId based on name
    for asg in asgData['AutoScalingGroups']:
        for tag in asg['Tags']:
            if tag['Key'] == 'Name':
                if tag['Value'].split(' ')[0] == clusterName:
                    return tag['ResourceId']

    else:
        print 'auto scaling group for {} not found. exiting'.format(clusterName)


def ec2_avg_cpu_utilization(clusterName, asgclient, cwclient):
    asg = find_asg(clusterName, asgclient)
    response = cwclient.get_metric_statistics( 
        Namespace='AWS/EC2',
        MetricName='CPUUtilization',
        Dimensions=[
            {
                'Name': 'AutoScalingGroupName',
                'Value': asg
            },
        ],
        StartTime=datetime.datetime.utcnow() - datetime.timedelta(seconds=120),
        EndTime=datetime.datetime.utcnow(),
        Period=60,
        Statistics=['Average']
    )
    return response['Datapoints'][0]['Average']


def empty_instances(clusterArn, activeContainerDescribed):
    # returns a object of empty instances in cluster
    instances = []
    empty_instances = {}

    for inst in activeContainerDescribed['containerInstances']:
        if inst['runningTasksCount'] == 0 and inst['pendingTasksCount'] == 0:
            empty_instances.update({inst['ec2InstanceId']: inst['containerInstanceArn']})

    return empty_instances


def draining_instances(clusterArn, drainingContainerDescribed):
    # returns an object of draining instances in cluster
    instances = []
    draining_instances = {} 

    for inst in drainingContainerDescribed['containerInstances']:
        draining_instances.update({inst['ec2InstanceId']: inst['containerInstanceArn']})

    return draining_instances


def terminate_decrease(instanceId, asgClient):
    # terminates an instance and decreases the desired number in its auto scaling group
    # [ only if desired > minimum ]
    try:
        response = asgClient.terminate_instance_in_auto_scaling_group(
            InstanceId=instanceId,
            ShouldDecrementDesiredCapacity=True
        )
        print response['Activity']['Cause']

    except Exception as e:
        print 'Termination failed: {}'.format(e)


def scale_in_instance(clusterArn, activeContainerDescribed):
    # iterates over hosts, finds the least utilized:
    # The most under-utilized memory and minimum running tasks
    # return instance obj {instanceId, runningInstances, containerinstanceArn}
    instanceToScale = {'id': '', 'running': 0, 'freemem': 0}
    for inst in activeContainerDescribed['containerInstances']:
        for res in inst['remainingResources']:
            if res['name'] == 'MEMORY':
                if res['integerValue'] > instanceToScale['freemem']:
                    instanceToScale['freemem'] = res['integerValue']
                    instanceToScale['id'] = inst['ec2InstanceId']
                    instanceToScale['running'] = inst['runningTasksCount']
                    instanceToScale['containerInstanceArn'] = inst['containerInstanceArn']
                    
                elif res['integerValue'] == instanceToScale['freemem']:
                    # Two instances with same free memory level, choose the one with less running tasks
                    if inst['runningTasksCount'] < instanceToScale['running']:
                        instanceToScale['freemem'] = res['integerValue']
                        instanceToScale['id'] = inst['ec2InstanceId']
                        instanceToScale['running'] = inst['runningTasksCount'] 
                        instanceToScale['containerInstanceArn'] = inst['containerInstanceArn']
                break

    print 'Scale candidate: {} with free {}'.format(instanceToScale['id'], instanceToScale['freemem'])
    return instanceToScale

    
def running_tasks(instanceId, containerDescribed):
    # return a number of running tasks on a given ecs host
    for inst in containerDescribed['containerInstances']:
        if inst['ec2InstanceId'] == instanceId:
            return int(inst['runningTasksCount']) + int(inst['pendingTasksCount']) 
    
    else:
        print 'Instance not found'


def drain_instance(containerInstanceId, ecsClient, clusterArn):
    # put a given ec2 into draining state
    try:
        response = ecsClient.update_container_instances_state(
            cluster=clusterArn,
            containerInstances=[containerInstanceId],
            status='DRAINING'
        )

    except Exception as e:
        print 'Draining failed: {}'.format(e) 


def future_reservation(activeContainerDescribed, clusterMemReservation):
    # If the cluster were to scale in an instance, calculate the effect on mem reservation
    # return cluster_mem_reserve*num_of_ec2 / num_of_ec2-1
    numOfEc2 = len(activeContainerDescribed['containerInstances'])
    if numOfEc2 > 1:
        futureMem = (clusterMemReservation*numOfEc2) / (numOfEc2-1)
    else:
        return 100

    print '*** Current: {} | Future : {}'.format(clusterMemReservation, futureMem)

    return futureMem


def asg_scaleable(asgData, clusterName):
    asg = find_asg(clusterName, asgData)
    for group in asgData['AutoScalingGroups']:
        if group['AutoScalingGroupName'] == asg:
            return True if group['MinSize'] < group['DesiredCapacity'] else False
    else:
        print 'Cannot find AutoScalingGroup to verify scaleability'
        return False


def retrieve_cluster_data(ecsClient, cwClient, asgClient, cluster):
    clusterName = cluster.split('/')[1]
    print '*** {} ***'.format(clusterName)
    activeContainerInstances = ecsClient.list_container_instances(cluster=cluster, status='ACTIVE')
    clusterMemReservation = cluster_memory_reservation(cwClient, clusterName)
    
    if activeContainerInstances['containerInstanceArns']:
        activeContainerDescribed = ecsClient.describe_container_instances(cluster=cluster, containerInstances=activeContainerInstances['containerInstanceArns'])
    else: 
        print 'No active instances in cluster'
        return False 
    drainingContainerInstances = ecsClient.list_container_instances(cluster=cluster, status='DRAINING')
    if drainingContainerInstances['containerInstanceArns']: 
        drainingContainerDescribed = ecsClient.describe_container_instances(cluster=cluster, containerInstances=drainingContainerInstances['containerInstanceArns'])
        drainingInstances = draining_instances(cluster, drainingContainerDescribed)
    else:
        drainingInstances = {}
        drainingContainerDescribed = [] 
    emptyInstances = empty_instances(cluster, activeContainerDescribed)

    dataObj = { 
        'clusterName': clusterName,
        'clusterMemReservation': clusterMemReservation,
        'activeContainerDescribed': activeContainerDescribed,
        'drainingInstances': drainingInstances,
        'emptyInstances': emptyInstances,
        'drainingContainerDescribed': drainingContainerDescribed        
    }

    return dataObj


def main(run='normal'):
    ecsClient = boto3.client('ecs')
    cwClient = boto3.client('cloudwatch')
    asgClient = boto3.client('autoscaling')
    asgData = asgClient.describe_auto_scaling_groups()
    clusterList = clusters(ecsClient)

    for cluster in clusterList:
        ########### Cluster data retrival ##########
        clusterData = retrieve_cluster_data(ecsClient, cwClient, asgClient, cluster)
        if not clusterData:
            continue
        else:
            clusterName = clusterData['clusterName']
            clusterMemReservation = clusterData['clusterMemReservation']
            activeContainerDescribed = clusterData['activeContainerDescribed']
            drainingInstances = clusterData['drainingInstances']
            emptyInstances = clusterData['emptyInstances']
        ########## Cluster scaling rules ###########
        if (clusterMemReservation < FUTURE_MEM_TH and 
           future_reservation(activeContainerDescribed, clusterMemReservation) < FUTURE_MEM_TH): 
        # Future memory levels allow scale
            if emptyInstances.keys():
            # There are empty instances                
                for instanceId, containerInstId in emptyInstances.iteritems():
                    if run == 'dry':
                        print 'Would have drained {}'.format(instanceId)  
                    else:
                        print 'I am draining {}'.format(instanceId)
                        drain_instance(containerInstId, ecsClient, cluster)

            if (clusterMemReservation < SCALE_IN_MEM_TH):
            # Cluster mem reservation level requires scale
                if (ec2_avg_cpu_utilization(clusterName, asgClient, cwClient) < SCALE_IN_CPU_TH):
                    instanceToScale = scale_in_instance(cluster, activeContainerDescribed)['containerInstanceArn']
                    if run == 'dry':
                        print 'Would have scaled {}'.format(instanceToScale)  
                    else:
                        print 'Going to scale {}'.format(instanceToScale)
                        drain_instance(instanceToScale, ecsClient, cluster)
                else:
                    print 'CPU higher than TH, cannot scale'
                

        if drainingInstances.keys():
        # There are draining instsnces to terminate
            for instanceId, containerInstId in drainingInstances.iteritems():
                if not running_tasks(instanceId, clusterData['drainingContainerDescribed']):
                    if run == 'dry':
                        print 'Would have terminated {}'.format(instanceId)
                    else:
                        print 'Terminating draining instance with no containers {}'.format(instanceId)
                        terminate_decrease(instanceId, asgClient)
                else:
                    print 'Draining instance not empty'

        print '***'

def lambda_handler(event, context):
    main()
    
