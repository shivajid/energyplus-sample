#!/usr/bin/env python

# for client launching
import socket
import subprocess
import argparse

import os
from os.path import exists, join
import platform

# for graphing
import plotly.offline as py
import plotly.graph_objs as go
import numpy as np

# for AI
from bonsai import Simulator, run_for_training_or_prediction
from bonsai.simulator import SimState

MAINVERSION = 2  # from defines.h
host = "localhost"
port = 0


def get_host():
    if platform.system() == "Windows":
        return ""
    else:
        return socket.gethostbyname(socket.gethostname())


def check_environ_vars():
    if not os.path.exists(get_cclient_path()):
        raise RuntimeError("cclient not found. Check the BCVTB_CCLIENT_PATH "
                           "environment variable.")
    if not os.path.exists(get_energyplus_path()):
        raise RuntimeError("energyplus not found. Check the ENERGYPLUS_BIN "
                           "environment variable.")


def get_bcvtb_path():
    return os.environ["BCVTB_HOME"]


def get_cclient_path():
    """ Get the path to the CCLIENT binary distributed with BCVTB. You can set
        this as environment variable, BCVTB_CCLIENT_BIN, before running this 
        program.
    """
    if "BCVTB_CCLIENT_BIN" in os.environ:
        return os.environ["BCVTB_CCLIENT_BIN"]
    elif platform.system() == "Windows":
        return "C:\\bcvtb\\examples\\c-room\\"
    else:
        return "./"


def get_energyplus_path():
    return os.environ["ENERGYPLUS_BIN"]


class Model(object):
    """ Base class for simulation models
    """

    def __init__(self, shellCmd):
        self.currentSimTime = 0.
        self.exitFlag = 0.
        self.process = None
        self.fromClient = None
        self.shellCmd = shellCmd

    def controller(self):
        return [0.]

    def grapher(self):
        pass

    def start(self):
        # must have
        # export BCVTB_HOME="${HOME}/bcvtb"
        # set in .bash_profile so that variables.dtd can be found
        self.currentSimTime = 0.
        self.exitFlag = 0
        self.fromClient = None
        self.process = subprocess.Popen(self.shellCmd, shell=True)

    def stop(self):
        self.process.terminate()
        self.exitFlag = 1   # because well, we're exited


class PtolemyServer(object):
    hostname = get_host()

    def __init__(self, model):

        # start parameters
        self.server_address = (self.hostname, 0)  # get new random port number
        self.model = model

        # open and setup the socket
        socket_true = 1
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET,
                             socket.SO_KEEPALIVE,
                             socket_true)
        self.sock.setblocking(True)

    def start(self):
        self.sock.bind(self.server_address)
        self.sock.listen(1)

        # write new config so client will connect to our port
        new_port = self.sock.getsockname()[1]
        socket_cfg_str = (
            "<?xml version=\"1.0\" encoding=\"ISO-8859-1\"?>\n"
            "<BCVTB-client>\n"
            " <ipc>\n"
            "    <socket port=\"{port}\" hostname=\"{hostname}\"/>\n"
            "  </ipc>\n"
            "</BCVTB-client>\n").format(hostname=self.hostname, port=new_port)

        try:
            config_file = open("socket.cfg", "w")
            config_file.write(socket_cfg_str)
            config_file.close()
        except OSError as msg:
            print("PtolemyServer: error writing socket.cfg:", msg)

        print(("PtolemyServer: "
               "server listening on {0}:{1}").format(self.server_address[0],
                                                     new_port))

        # start the model now...
        self.model.start()

    def waitForClient(self):
        print("PtolemyServer: waiting for client...")
        self.conn, self.address = self.sock.accept()
        print("PtolemyServer: got a connection from:", self.address)

    def readFromClient(self):
        buffer = self.conn.recv(4096).decode('ascii')
        params = buffer.split()
        # print("PtolemyServer recv:", buffer)

        self.model.exitFlag = 1    # exit if no params received
        if len(params) >= 2:
            version = int(params[0])
            if version == MAINVERSION:
                self.model.exitFlag = int(params[1])

                # if the exit flag hasn't been sent, read the rest
                if self.model.exitFlag == 0:

                    # parse the remainder
                    clientDoubleCount = int(params[2])
                    # clientIntCount = int(params[3])
                    # clientBoolCount = int(params[4])
                    currentSimTime = float(params[5])
                    fromClient = []
                    for n in range(clientDoubleCount):
                        fromClient.append(float(params[6+n]))

                    # run the controller
                    self.model.fromClient = fromClient
                    self.model.currentSimTime = currentSimTime

                    print("PtolemyServer recv:", fromClient)
                else:
                    print("PtolemyServer: got exit request")
            else:
                print("PtolemyServer: ERROR: unkown ptolemy packet version")
        else:
            print("PtolemyServer: ERROR: not enough ptolemy packet variables")

    def writeToClient(self, responseDoubles):
        if self.model.exitFlag == 0:
            # compose a response
            responseDoubleCount = len(responseDoubles)
            responseIntCount = 0
            responseBoolCount = 0
            response = "{0:d} {1:d} {2:d} {3:d} {4:d} {5:g} ".format(
                                                     MAINVERSION,
                                                     self.model.exitFlag,
                                                     responseDoubleCount,
                                                     responseIntCount,
                                                     responseBoolCount,
                                                     self.model.currentSimTime)
            for d in responseDoubles:
                response += "{0:g} ".format(d)
            response += "\n"
        else:
            response = "{0:d} {1:d}\n".format(MAINVERSION, self.model.exitFlag)

        # write the response
        print("PtolemyServer send:", responseDoubles)
        self.conn.sendall(response.encode('ascii'))

    def close(self):
        try:
            self.model.stop()
            print("PtolemyServer: terminate model process")
        except OSError as msg:
            print("PtolemyServer: error closing model process:", msg)

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
            self.sock.close()
            print("PtolemyServer: closed")
        except OSError as msg:
            print("PtolemyServer: error on close:", msg)


class CRoom(Model):
    cclient_path = os.path.join(get_cclient_path(),
                                ("cclient.exe"
                                 if platform.system() == "Windows"
                                 else "cclient"))

    def __init__(self):
        Model.__init__(self, "{0} 60".format(self.cclient_path))

        # we expect 4 doubles from the client
        self.data = ([], [], [], [])

        # model variables from the the bcvtb example `c-room`
        # Kp is originally represented as a matrix, but we don't need to do 
        # that here
        self.Kp = [1., 7.5]
        self.TSet = [18., 20.]
        self.yIni = [0., 0.]

    def controller(self):
        # ripple the sample delay
        response = self.yIni

        # control
        values = self.yIni   # so sizes match
        for n in range(2):
            values[n] = self.TSet[n] - self.fromClient[n]    # feedback
            values[n] = values[n] * self.Kp[n]          # gain
            self.yIni[n] = min(1., max(0., values[n]))  # clamp to [0..1]

        # save the client input in our graph
        for n in range(2):
            self.data[n].append(self.fromClient[n])
            self.data[n+2].append(response[n] * 10)

        # package response to the client
        return response

    def grapher(self):
        xAxis = np.linspace(0,
                            self.currentSimTime,
                            num=len(self.data[0]),
                            endpoint=False)
        trace0 = go.Scatter(x=xAxis,
                            y=self.data[0],
                            mode='lines',
                            name='TRoom1')
        trace1 = go.Scatter(x=xAxis,
                            y=self.data[1],
                            mode='lines',
                            name='TRoom2')
        trace2 = go.Scatter(x=xAxis,
                            y=self.data[2],
                            mode='lines',
                            name='10*y1')
        trace3 = go.Scatter(x=xAxis,
                            y=self.data[3],
                            mode='lines', 
                            name='10*y2')
        return [trace0, trace1, trace2, trace3]


class ePlus85Actuator(Model):
    """ Eplus85-actuator model """
    energyplus = join(get_energyplus_path(),
                      "energyplus.exe" if platform.system() == "Windows"
                      else "energyplus")
    weather = join(get_bcvtb_path(),
                   "examples/ePlusWeather/"
                   "USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw")
    datafile = "EMSWindowShadeControl.idf"

    def __init__(self):
        Model.__init__(self,
                       "{energyplus} -w {weather} -p output -s C -x -m "
                       "-r {datafile}".format(energyplus=self.energyplus,
                                              weather=self.weather,
                                              datafile=self.datafile))

        # we expect 4 doubles from the client
        self.data = ([], [], [], [], [])

        # model variables from the the bcvtb example `eplus85-actuator`
        self.yShade = 6.  # 0 or 6

    def controller(self):
        # save the client input in our graph
        for n in range(len(self.fromClient)):
            value = self.fromClient[n]

            # scale some of the values for readability
            if n == 2:
                value /= 100.

            self.data[n].append(value)

        self.data[4].append(self.yShade)
        return [self.yShade]

    def grapher(self):
        xAxis = np.linspace(0,
                            self.currentSimTime,
                            num=len(self.data[0]),
                            endpoint=False)
        trace0 = go.Scatter(x=xAxis, y=self.data[0], mode='lines', name='TOut')
        trace1 = go.Scatter(x=xAxis,
                            y=self.data[1],
                            mode='lines',
                            name='TZone')
        trace2 = go.Scatter(x=xAxis,
                            y=self.data[2],
                            mode='lines',
                            name='SolarIrradiation/100')
        trace3 = go.Scatter(x=xAxis,
                            y=self.data[3],
                            mode='lines',
                            name='FractionShadingOn')
        trace4 = go.Scatter(x=xAxis,
                            y=self.data[4],
                            mode='lines',
                            name='Reward')
        return [trace0, trace1, trace2, trace3, trace4]


def write_graph(graph):

    div = py.plot(graph, auto_open=False, output_type='div', show_link=False)
    output_html = ("<html>"
                   "<head><META HTTP-EQUIV=\"refresh\" CONTENT=\"5\"></head>"
                   "<body>{0}</body>"
                   "</html>").format(div)
    try:

        config_file = open("graph.html", "w")
        config_file.write(output_html)
        config_file.close()
    except OSError as msg:
        print("PtolemyServer: error writing graph.html:", msg)


def test_model(model):

    for runs in range(4):
        # launch the client...
        server = PtolemyServer(model)
        server.start()

        # ...wait for it to connect
        server.waitForClient()

        # initial read
        server.readFromClient()

        # run simulation loop
        print("test_model: starting simulation loop")

        n = 0
        while model.exitFlag == 0:
            n += 1

            try:
                response = model.controller()
                server.writeToClient(response)
                server.readFromClient()

            except OSError as msg:
                print(msg)
                break
        # close the connectionon.
        server.close()

        # write a graph out for the last one...
        graph = model.grapher()
        write_graph(graph)


class EnergyPlusSimulator(Simulator):
    model = ePlus85Actuator()
    server = None

    clientState = {'SolarIrradiation': 0}
    shade = 0.
    is_terminal = True

    def start(self):
        """This method is called when training is started."""
        print("EnergyPlusSimulator: start")

    def stop(self):
        print("EnergyPlusSimulator: stop")

        graph = self.model.grapher()
        py.plot(graph, filename="graph.html")

    def readFromPtolemyClient(self):
        self.server.readFromClient()
        if self.model.fromClient and len(self.model.fromClient) == 4:
            self.clientState = {
                # 'TOut': self.model.fromClient[0],
                # 'TZone': self.model.fromClient[1],
                'SolarIrradiation': int(self.model.fromClient[2])/100
                # 'FractionShadingOn': self.model.fromClient[3]
                }

            # save the client input in our graph
            for n in range(len(self.model.fromClient)):
                value = self.model.fromClient[n]
                # scale some of the values for readability
                if n == 2:
                    value /= 100.
                self.model.data[n].append(value)

        self.is_terminal = self.model.exitFlag != 0

    def restartPtolemyServer(self):
        # set some default values for get_state
        self.is_terminal = True
        # self.clientState = {'TOut': 0.,
        #                     'TZone': 0.,
        #                     'SolarIrradiation': 0.,
        #                     'FractionShadingOn': 0. }
        self.clientState = {'SolarIrradiation': 0}

        # close the old connections if they're still open
        if self.server:
            self.server.close()

        # star a new episode
        print("EnergyPlusSimulator: starting PtolemyServer")
        self.server = PtolemyServer(self.model)

        try:
            self.server.start()
            self.server.waitForClient()
            # get initial state
            self.readFromPtolemyClient()

        except OSError as msg:
            print("EnergyPlusSimulator: error on restart:", msg)
            self.server = None

    def reset(self):
        """Called by the AI Engine to reset simulator state in between training
           and test passes.
        """
        print("EnergyPlusSimulator: reset")

    def advance(self, actions):
        print("EnergyPlusSimulator: advance ", actions)
        """Advance the simulation forward one tick. actions contains a
           dictionary of key values as defined by this simulator's action
           schema in Inkling.
        """
        self.shade = actions['shade'] * 6.  # Int32[0..1]

    def set_properties(self, **kwargs):
        print("EnergyPlusSimulator: set_properties")
        """This method is called before training is started
           or on the frame after is_terminal=True to set
           configuration properties in this simulation. See
           the configure clause of the lesson statement in
           this simulator's accompanying curriculums.
        """
        self.restartPtolemyServer()

    def get_state(self):
        """Returns a named tuple of state and is_terminal. state is a
           dictionary matching the state schema as defined in Inkling.
           is_terminal is only true when the simulator is in a "game over"
           state.
        """

        print("EnergyPlusSimulator: get_state: terminal:", self.is_terminal)

        if self.is_terminal:
            self.restartPtolemyServer()
        else:
            self.server.writeToClient([self.shade])
            self.readFromPtolemyClient()

        # you like graphs? WE HAVE GRAPHS. SO MANY GRAPHS.
        if self.is_terminal:
            graph = self.model.grapher()
            write_graph(graph)

            # clear old data
            self.model.data = ([], [], [], [], [])

        return SimState(state=self.clientState, is_terminal=self.is_terminal)

    def reward_function(self):
        print("EnergyPlusSimulator: reward_function")
        # largest reward is best reward (maximize)
        reward = 0.
        if self.model.fromClient and len(self.model.fromClient) == 4:
            # SolarIrradiation === Shades down === good
            # TOut = self.model.fromClient[0]
            SolarIrradiation = self.model.fromClient[2] / 100.

            # sun is down
            if SolarIrradiation <= 1:
                if self.shade > 0:
                    reward = -1  # shades on
                else:
                    reward = 1  # shade off

            # sun is out
            else:
                if self.shade > 0: 
                    reward = 1  # shades on
                else:
                    reward = -1  # shades off

            self.model.data[4].append(reward)

        print("EnergyPlusSimulator reward:", reward)
        return reward


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='Launch EnergyPlus simulator')
    parser.add_argument('--test_croom', action='store_true', default=False)
    parser.add_argument('--test_energyplus',
                        action='store_true',
                        default=False)
    parser.add_argument('--predict-brain')
    parser.add_argument('--train-brain')
    parser.add_argument('--predict-version')
    args = parser.parse_args()

    # Test the results from the model or from the AI
    if args.test_croom or args.test_energyplus:
        if args.test_croom:
            test_model(model=CRoom())
        elif args.test_energyplus:
            test_model(model=ePlus85Actuator())
    else:
        run_for_training_or_prediction(name="energyplus_simulator",
                                  simulator_or_generator=EnergyPlusSimulator())
