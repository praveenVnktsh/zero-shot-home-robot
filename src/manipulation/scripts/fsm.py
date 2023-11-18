#! /usr/bin/env python3

from manip_basic_control import ManipulationMethods
from visual_servoing import AlignToObject
import rospy
import actionlib
from enum import Enum
import numpy as np
from grasp_detector.msg import GraspPoseAction, GraspPoseResult, GraspPoseGoal
from manipulation.msg import TriggerAction, TriggerFeedback, TriggerResult, TriggerGoal
import json
from control_msgs.msg import FollowJointTrajectoryAction
from plane_detector.msg import PlaneDetectAction, PlaneDetectResult, PlaneDetectGoal
from helpers import move_to_pose
from std_srvs.srv import Trigger, TriggerResponse
from scene_parser import SceneParser

class States(Enum):
    IDLE = 0
    VISUAL_SERVOING = 1
    PICK = 2
    WAITING_FOR_GRASP_AND_PLANE = 3
    WAITING_FOR_GRASP = 4
    WAITING_FOR_PLANE = 5
    PLACE = 6
    COMPLETE = 7

class ManipulationFSM:

    def __init__(self, loglevel = rospy.INFO):

        rospy.init_node('manipulation_fsm', anonymous=True, log_level=loglevel)
        self.state = States.IDLE
        self.grasp = None
        self.planeOfGrasp = None

        self.scene_parser = SceneParser()
        self.class_list = rospy.get_param('/object_detection/class_list')
        self.label2name = {i : self.class_list[i] for i in range(len(self.class_list))}

        self.offset_dict = rospy.get_param('/manipulation/offsets')
        self.isContactDict = rospy.get_param('/manipulation/contact')
        
        self.n_max_servo_attempts = rospy.get_param('/manipulation/n_max_servo_attempts')   
        self.n_max_pick_attempts = rospy.get_param('/manipulation/n_max_pick_attempts')

        self.server = actionlib.SimpleActionServer('manipulation_fsm', TriggerAction, execute_cb=self.main, auto_start=False)

        self.trajectoryClient = actionlib.SimpleActionClient('alfred_controller/follow_joint_trajectory', FollowJointTrajectoryAction)
        self.graspActionClient = actionlib.SimpleActionClient('grasp_detector', GraspPoseAction)
        self.planeFitClient = actionlib.SimpleActionClient('plane_detector', PlaneDetectAction)


        rospy.loginfo(f"[{rospy.get_name()}]:" + "Waiting for trajectoryClient server...")
        self.trajectoryClient.wait_for_server()


        # DEPRECATED
        # rospy.loginfo(f"[{rospy.get_name()}]:" + "Waiting for grasp_detector server...")
        # self.graspActionClient.wait_for_server()

        # rospy.loginfo(f"[{rospy.get_name()}]:" + "Waiting for plane_detector server...")
        # self.planeFitClient.wait_for_server()


        rospy.loginfo(f"[{rospy.get_name()}]:" + "Waiting for stow_robot service...")
        self.stow_robot_service = rospy.ServiceProxy('/stow_robot', Trigger)
        self.stow_robot_service.wait_for_service()

        self.visualServoing = AlignToObject(self.scene_parser)
        self.manipulationMethods = ManipulationMethods()
        
        
        self.server.start() # start server only after everything under this is initialized
        rospy.loginfo(f"[{rospy.get_name()}]:" + "Node Ready.")
    
    def send_feedback(self, info):
        feedback = TriggerFeedback()
        feedback.curState = self.state.value
        feedback.curStateInfo = json.dumps(info)
        rospy.loginfo(f"[{rospy.get_name()}]:" + json.dumps(info))
        self.server.publish_feedback(feedback)

    def reset(self):
        self.state = States.IDLE
        self.grasp = None
        self.planeOfGrasp = None

    def main(self, goal : TriggerGoal):
        self.goal = goal
        self.state = States.IDLE
        objectManipulationState = States.PICK

        self.scene_parser.set_object_id(goal.objectId)
        
        rospy.loginfo(f"{rospy.get_name()} : Stowing robot.")
        
        # self.stow_robot_service()
        if goal.isPick:
            rospy.loginfo("Received pick request.")
            objectManipulationState = States.PICK
        else:
            rospy.loginfo("Received place request.")
            objectManipulationState = States.PLACE

        nServoTriesAttempted = 0
        nServoTriesAllowed = self.n_max_servo_attempts

        nPickTriesAttempted = 0
        nPickTriesAllowed = self.n_max_pick_attempts

        # self.manipulationMethods.move_to_pregrasp(self.trajectoryClient)
        
        # self.scene_parser.set_point_cloud(publish = True) #converts depth image into point cloud
        # grasp = self.scene_parser.get_grasp(publish = True)
        # plane = self.scene_parser.get_plane(publish = True)
        # print(grasp)
        # ee_pose = self.manipulationMethods.getEndEffectorPose()
        # self.visualServoing.alignObjectHorizontal(ee_pose_x = ee_pose[0] - 0.07, debug_print = {"ee_pose" : ee_pose})
        # self.visualServoing.alignObjectHorizontal()
        # self.scene_parser.set_point_cloud(publish = True) #converts depth image into point cloud
        # grasp = self.scene_parser.get_grasp(publish = True)
        # plane = self.scene_parser.get_plane(publish = False)
        # print(grasp)
        # print(plane)
        # exit() 
        self.state = States.PICK   

        try: 
            while True:
                if self.state == States.IDLE:
                    self.state = States.VISUAL_SERVOING
                    if objectManipulationState == States.PLACE:
                        self.send_feedback({'msg' : "Trigger Request received. Placing"})
                        self.state = States.PLACE
                    else:
                        self.send_feedback({'msg' : "Trigger Request received. Starting to find the object"})
                elif self.state == States.VISUAL_SERVOING:
                    success = self.visualServoing.main(goal.objectId,)
                    if success:
                        self.send_feedback({'msg' : "Servoing succeeded! Starting manipulation."})
                        self.state = objectManipulationState
                    else:
                        if nServoTriesAttempted >= nServoTriesAllowed:
                            self.send_feedback({'msg' : "Servoing failed. Aborting."})
                            self.reset()
                            return TriggerResult(success = False)
                        
                        self.send_feedback({'msg' : "Servoing failed. Attempting to recover from failure."  + str(nServoTriesAttempted) + " of " + str(nServoTriesAllowed) + " allowed."})
                        nServoTriesAttempted += 1
                        self.visualServoing.recoverFromFailure()

                elif self.state == States.PICK:
                    self.state = States.WAITING_FOR_GRASP_AND_PLANE
                    self.send_feedback({'msg' : "moving to pregrasp pose"})
                    
                    
                    # basic planning here
                    self.manipulationMethods.move_to_pregrasp(self.trajectoryClient)
                    ee_pose = self.manipulationMethods.getEndEffectorPose()
                    self.visualServoing.alignObjectHorizontal(ee_pose_x = ee_pose[0], debug_print = {"ee_pose" : ee_pose})

                    self.scene_parser.set_point_cloud(publish = True) #converts depth image into point cloud
                    grasp = self.scene_parser.get_grasp(publish = True)
                    # plane = self.scene_parser.get_plane(publish = True)
                    
                    
                    if grasp and plane:
                        plane_height = plane[1]
                        grasp_center, grasp_yaw = grasp
                        self.heightOfObject = abs(grasp_center[2] - plane_height)
                        
                        offsets = self.offset_dict[self.label2name[goal.objectId]]
                        offsets[1] -= 0.02 #constant safety factor
                        grasp = (grasp_center + np.array(offsets)), grasp_yaw
                        self.manipulationMethods.pick(
                            self.trajectoryClient, 
                            grasp,
                            moveUntilContact = self.isContactDict[self.label2name[goal.objectId]]
                        ) 
                        
                        success = self.manipulationMethods.checkIfGraspSucceeded()
                        if success:
                            self.send_feedback({'msg' : "Pick succeeded! Starting to place."})
                            self.state = States.COMPLETE
                        else:
                            self.send_feedback({'msg' : "Pick failed. Reattempting."})
                            if nPickTriesAttempted >= nPickTriesAllowed:
                                self.send_feedback({'msg' : "Pick failed. Cannot grasp successfully. Aborting."})
                                self.reset()
                                return TriggerResult(success = False)
                            
                            self.send_feedback({'msg' : "Picking failed. Reattempting pick, try number " + str(nPickTriesAttempted) + " of " + str(nPickTriesAllowed) + " allowed."})
                            nPickTriesAttempted += 1

                elif self.state == States.PLACE:
                    heightOfObject = goal.heightOfObject
                    move_to_pose(
                        self.trajectoryClient,
                        {
                            'head_pan;to' : -np.pi/2,
                            'base_rotate;by': np.pi/2,
                        }
                    )
                    rospy.sleep(5)
                    self.scene_parser.set_point_cloud() #converts depth image into point cloud
                    
                    plane = self.scene_parser.get_plane()
                    if plane:
                        plane_height = plane[1]
                        xmin, xmax, ymin, ymax = plane[2]
                        
                        if ymin * ymax < 0: # this means that it is safe to place without moving base
                            placingLocation = np.array([0.0, (xmin + xmax)/2, plane_height + heightOfObject + 0.1])
                            
                        placingLocation = np.array([(ymin + ymax)/2, (xmin + xmax)/2, plane_height + heightOfObject + 0.1])

                        self.manipulationMethods.place(self.trajectoryClient, placingLocation[0], placingLocation[1], placingLocation[2], 0)

                        self.stow_robot_service()
                    self.state = States.COMPLETE

                elif self.state == States.COMPLETE:
                    # self.send_feedback({'msg' : "Work complete successfully."})
                    rospy.loginfo(f"{rospy.get_name()} : Work complete successfully.")
                    move_to_pose(self.trajectoryClient, {
                        "head_pan;to" : 0,
                    })
                    break

        except KeyboardInterrupt:
            print("User Exited!!")

        # self.heightOfObject = abs(self.grasp.z - self.planeOfGrasp.z) 
        self.heightOfObject = 0
        self.reset()
        self.server.set_succeeded(result = TriggerResult(success = True, heightOfObject = self.heightOfObject))



if __name__ == '__main__':
    node = ManipulationFSM()
    try:
        rospy.spin()
    except KeyboardInterrupt:
        print("Shutting down")

