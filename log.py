#!/usr/bin/python3

import http.server
from urllib.parse import urlparse
from urllib.parse import parse_qs
from urllib.parse import parse_qsl
import socket
import argparse
import time
import mmap
import os
import json
import threading
import struct
import requests

def get_self_ip():
   s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
   s.connect( ("1.1.1.1",1))
   ip = s.getsockname()[0]
   host = socket.gethostname()
   return (host,ip)

# simple UDP multicasting functionality to identify other services listening on the same multicast
# group
class multicasting:
   send_id =  "789A79F1FD517771"

   def __init__(self, directory, group, port, log_port):
      print("creating log group multicaster at", group,":",port)
      self.directory = directory
      self.group = group
      self.port = port
      self.logger_port = log_port
      self.mutex = threading.Lock()
      self.halt = False
      self.log_servers = {}
      
   def send(self):
      print("starting multicast send thread")
      send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
      send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)      

      host,ip = get_self_ip()
      data={}
      data["host"]=host
      data["ip"]=ip
      data["port"]=self.logger_port

      while True and self.halt==False:
         send_sock.sendto(bytes(json.dumps(data),"utf-8"), (self.group,self.port))
         # TODO - make sleep() configurable
         time.sleep(1)

   def receive(self):
      print("starting multicast receive thread")
      recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      recv_sock.bind( ('',self.port) )
      group = socket.inet_aton(self.group)
      r = struct.pack('4sl', group, socket.INADDR_ANY)
      recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, r)

      grp_addr = (self.group,self.port)

      while True and self.halt==False:
         data,from_addr = recv_sock.recvfrom(64)
         js=json.loads(data)
         self.mutex.acquire()
         try:
            # this does not handle if a former listener is down
            # and is beyond the scope of this project
            self.log_servers[js["ip"]+":"+str(js["port"])]=js
            
         finally:
            self.mutex.release()
 

   def get_servers(self):
      self.mutex.acquire()
      try:
         js = json.dumps(self.log_servers)
      finally:
         self.mutex.release()

      return js

   def start(self):
      print("starting all multicast threads")
      self.send_thread = threading.Thread(target=self.send, daemon=True)
      self.recv_thread = threading.Thread(target=self.receive, daemon=True)

      self.send_thread.start()
      self.recv_thread.start()

   def shutdown(self):
      self.send_thread.join()
      self.recv_thread.join()
  
# handles the parsing of the boolean filters 
class simple_filter_parser:

   # filter_list - array of filters
   #ie: ["and|term term term", "or|term term", "term term term"]
   def __init__(self, filter_list):
      self.operators = []
      self.terms = []
      
      def proc(filter):
         # filter format: (optional)OPERATOR|term1 term2 term3 .. termN
         # ie: or|critical failure error timeout   
         # ie: and|500 internal server custid 4349109566  <--explicit AND
         # ie: custid 41982342 xid ajf033epn35  <--implicit AND without operator  
         idx=filter.find('|')
         if idx!=-1:
            self.operators.append(filter[:idx].lower().strip())
            self.terms.append(filter[idx+1:].split(' '))
         else: # default to AND operator
            self.terms.append(filter.split(' '))
            self.operators.append("and")
            
      for filter in filter_list:
         proc(filter)

      print("FilterParser:",self.operators,":",self.terms)

'''
 Request handler for all logging API calls
 The following API calls are provided:
 
 /log - retrieves the log file, performs optional simple boolean filtering
  parameters: 
     fn=[filename] - the log file desired
     n=[num] - "tail" number of lines to retrieve
     ftr=[and|andterm1+andterm2], ftr=[or|orterm1+orterm2+..+termN] - note:multiple ftr params
         can be given, ie /log?ftr=and|t1+t2+t3&ftr=or|t4+t5. The respective boolean phrases
         are "AND" together. So the above = (t1 and t2 and t3) AND (t4 or t5)
     r=[(t)rue|(f)alse] - short for routing. If set to 't' or 'true', this will attempt to redirect 
           the request to the server that has the given filename. Note that the filename can only 
           be 0 level deep from log directory the service is running on. 

 /ls - returns a list of files/folders
  parameters:
      fn=[path] - path from the base log folder. This allows to list files of subfolders
      g=[(t)rue|(f)alse] - globally query all logger services for their respective list files
                           from their log folders. Note that the 'fn' will be ignored since
                           each service may have different log folders or subfolder paths. Note
                           the api does not provide a visual means of distinguishing files and
                           folders at this time.
    Accept: text/plain  - returns space delimited list of files for local. For global returns
                          host_ip:port\n followed by space delimited list of files
    Accept: application/json - returns json {host,ip,files=[]} for local or array of such for global
'''
class logger(http.server.BaseHTTPRequestHandler):
   def __init__(self,port,log_dir,castgrp, castport):
      print("creating logger at:",port,log_dir,"multicast grp=",castgrp,"cast port=",castport)
      self.port = port
      self.log_dir = log_dir
      self.log_multicaster = multicasting(directory=log_dir, group=castgrp, 
         port=castport, log_port=port)

      self.log_multicaster.start()
      self.mutex = threading.Lock()

      self.poll_thread = threading.Thread(target=self.poll_loggers, daemon=True)
      self.poll_thread.start()
      self.files = {}
      self.servers = {}

   def __call__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)

   # call ls on other loggers to obtain file list - inverse it for quick server lookup   
   def poll_loggers(self):
      print("started logger poll thread")
      while True:
         js=self.log_multicaster.get_servers()
         self.mutex.acquire()
         try:
            self.svrs = json.loads(js)   
         finally:
            self.mutex.release()

         to_remove = []
         for svr in self.svrs.values():
            try:
               conn = http.client.HTTPConnection(svr["ip"],svr["port"])
               conn.request(method="GET", url="/ls", headers={"Accept":"text/plain"})
               resp = conn.getresponse()
               data = str(resp.read())
            
               files = data.split()
              
               for file in files:
                  self.files[file]=(svr["ip"],svr["port"])
                 
               self.servers[svr["ip"]+":"+str(svr["port"])]=files    
               conn.close()
            except Exception as e:
               print("logger at ",svr["ip"],svr["port"],"could not be reached, removing from pool")
               to_remove.append(svr["ip"]+":"+str(svr["port"]))

         for s in to_remove:
            self.svrs.pop(s,None)

         time.sleep(2)
       

   def send_ok(self,chunked,content):
      self.send_response(200)
      if content.lower()=="text":
         self.send_header("Content-type", "text/plain")
      elif content.lower()=="json":
         self.send_header("Content-type", "application/json")

      if chunked==True:
         self.send_header("Transfer-Encoding", "chunked")
      self.end_headers()

   def send_ok_data(self, content):
      self.send_response(200)
      self.send_header("Content-type", "text/plain")
      self.send_header("Content-Length", str(len(content)))
      self.end_headers()
      if isinstance(content, (bytes,bytearray)):
         self.wfile.write(content)
      elif isinstance(content, (str)):
         self.wfile.write(bytes(content,"utf-8"))

   def send_error(self,msg):
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", len(msg))
      self.end_headers()
      self.wfile.write(bytes(msg,"utf-8"))

   def chunk_send(self, msg, is_bytes=True):
      # send HEX msg length + \r\n as per chunked specs
      mlen = len(msg)
      slen = '{0:x}\r\n'.format(mlen)
       
      self.wfile.write(slen.encode(encoding='utf-8'))
      
      if isinstance(msg, (bytes,bytearray)):
         self.wfile.write(msg)
      else:
         self.wfile.write(bytes(msg,"utf-8"))

      self.wfile.write(bytes("\r\n","utf-8"))

   def chunk_end(self):
      self.wfile.write(bytes("0\r\n","utf-8"))
      self.wfile.write(bytes("\r\n","utf-8"))

   def do_GET(self):
      
      parsed = urlparse(self.path)
      params = parse_qs(parsed.query)

      print(parsed)
      print(params)
      print(self.headers)

      path = parsed.path

      # retrieve log
      if path == "/log":
         print("/log with ",params)
         if len(params) == 0:
            self.send_error("log call missing parameters\n")
            return

         num = -1 # all lines
         filename = ""
         filters = []
         route = "f"

         if "n" in params:
            num = int(params["n"][0])

         if "fn" in params:
            filename=params["fn"][0]
         else:
            self.send_error("a filename is required\n")
            return

         if "ftr" in params:
            filters=params["ftr"]
         
         if "r" in params:
            route=params["r"][0]

         name_ip = get_self_ip()

         if route.lower()=="true" or route.lower()=="t": 
            # redirect to server that has the file
            if filename in self.files.keys():
               server = self.files[filename]
               print("found:",filename," at server:", server)
               self.send_response(301)
               loc="http://"+server[0]+":"+str(server[1])+"/log?fn="+filename
               loc+="&n="+str(num)
               if len(filters)>0:
                  for filter in filters:
                     loc+="&ftr="
                     loc+=filter.replace(' ','+')

               self.send_header("Location",loc)
               self.end_headers()
               print(loc)
         else:
            print("retrieving local log ",filename,", ",num," lines using filter:",filters)
            self.get_log(filename, num, filters)

         return	
         # log file name, number of latest, filter string

      # list all the files in main log folder 
      if path == "/ls":
         folder=""
         if "fn" in params:
            folder = params["fn"][0]

         # g - global - list the files for every server in the logger network
         gv=False
         if "g" in params:
            v = params["g"][0]
            if v.lower()=="true" or v.lower()=="t":
               gv=True
          
         accept = self.headers.get("Accept")
         if self.headers.get("Accept") is None:
            accept="text/plain"

         self.get_ls_files(accept,folder,gv)
         return
     
      error_msg = path + " method not allowed for the requested URL.\n"

      self.send_error(error_msg)

   # list files either locally or globally
   def get_ls_files(self,accept_type,path,glbal):
      name_ip = get_self_ip()
      files = os.listdir(self.log_dir+path)

      if glbal==True:
         print("global ls requested of all servers")
         if accept_type == "application/json":
            dict = []
           
            for key in self.servers.keys():
               server = {}
               files = self.servers[key]
               key=key.split(':')
               server["ip"]=key[0]
               server["port"]=int(key[1])
               server["files"] = files
               dict.append(server)

            d=json.dumps(dict)
            self.send_ok_data(d)
         elif accept_type=="*/*" or accept_type =="text/plain":
            self.send_ok(chunked=True, content="text")

            for key in self.servers.keys():
               self.chunk_send(key+":\n")
               files = self.servers[key]
               for file in files:
                  file+='\n'
                  print("global ls sending:",file)
                  self.chunk_send(file)
               self.chunk_send("\n")
            self.chunk_end()   

         return

      if accept_type=="application/json":
         dict = {}

         dict["host"]=name_ip[0]
         dict["port"]=self.port
         dict["ip"]=name_ip[1]

         dict["files"] = files
  
         self.send_ok_data(json.dumps(dict))
 
      elif accept_type=="*/*" or accept_type=="text/plain":
         print("local ls command plain/text")
         try: 
            self.send_ok(chunked=True,content="text") 

            for file in files:
               file+=' '
               self.chunk_send(file)
      
            self.chunk_end()
         except Exception as e:
            print(e.__class__, ' ', e)

      else:
         print("unknown accept type:",accept_type)

   # filename - local path/logfile to search. Path is relative to main log folder
   # num - number of lines to search through. Note if filter is applied, count<num may be returned
   # search_filters - format:  optional(OPERATOR) | term1 term2 .. termN 
   #        ie: and|foo bar   or|foo bar    foo bar
   def get_log(self, filename, num, search_filters):
      print("get_log")

      parsed_filter = simple_filter_parser(search_filters)
      print(parsed_filter.terms)

      # open as memory mapped file 
      try:
         fp = open(self.log_dir+filename,"r+")
      except FileNotFoundError:
         self.send_error(self.log_dir+filename+" was not found\n")
         return

      mm = mmap.mmap(fp.fileno(),	0)

      self.send_ok(content="text", chunked=True)

      def check_filter(start,end):
         size = len(parsed_filter.operators)

         if size==0: return True

         acc_final = []

         # for each operator
         for i in range(size):
            acc_and = []
            acc_or = []
            operator = parsed_filter.operators[i]
            terms = parsed_filter.terms[i]

            for term in terms:
               i = mm[start:end].find(bytes(term,"utf-8"))

               if operator=="and":
                  if i==-1: 
                     #print("AND for",term,"at",start,":",end,"FAILED!")
                     acc_and.append(False)
                     break
                  else:
                     #print("Found AND:",term,"at ", start,":",end)
                     acc_and.append(True)

               if operator=="or":
                  if i!=-1: 
                     acc_or.append(True)
                     #print("Found OR:",term,"at ", start,":",end)
                     break;
                  else:
                     acc_or.append(False)


            if len(acc_and)==0: acc_and.append(True)
            if len(acc_or)==0: acc_or.append(True)

            if all(acc_and) and any(acc_or):
               #print(start,":",end,"PASSES filter","AND=",acc_and,",OR=",acc_or)
               acc_final.append(True)
            else:
               #print(start,":",end,"FAILS filter","AND=",acc_and,",OR=",acc_or)
               acc_final.append(False)

         if all(acc_final):
            return True

         return False
           

      # use reverse find of newline
      # keep track of start and end of each newline to create a search space for
      # the memory mapped file. Keeping all operations in mmap allows for arbitrary large
      # lines we can search without having to deal with memory constraints. For example
      # if we had, however unlikely, a single line that were multi GB in size, using this
      # approach still allows us to filter search terms with reasonable performance
      start = mm.size()

      if num != -1:
         end = mm.size()
         print("size of file = ",end)
         for i in range(num):
            start = mm.rfind(b"\n",0,end)
            if start!=-1 and start!=end:
               print("found newline at ",start)
               end=start
            
         print("found ",num," line at index ", start, " of file:",self.log_dir+filename)

      # search the whole file
      else:
         start=0

      # apply filters
      full_end = mm.size()
      end = mm.find(b"\n",start+1,full_end)
      if end==-1:
         end=mm.size()

      count=0

      while(count<num or end<=full_end):
         if check_filter(start,end)==True:
            self.chunk_send(msg=mm[start:end],is_bytes=True)
         
         if end==full_end: break
    
         start=end
         end = mm.find(b"\n",start+1,full_end)
   
         if end==-1: end=full_end

         if num!=-1:
            count+=1

      self.chunk_end()

      mm.close()
      fp.close()
			

if __name__ == "__main__":
   ap = argparse.ArgumentParser(description="log file service")
   ap.add_argument('-p', default='7777', type=int, metavar='PORT', help="port", dest='port')
   ap.add_argument('-d', default='/var/log', metavar='LOG_FOLDER_PATH', dest='log_path', 
      help="main log folder")
   ap.add_argument('-m', default='239.0.1.5', metavar='multicast IP', dest='multicast_grp', 
      help="multicast group")
   ap.add_argument('-g', default='8888', type=int, metavar='multicast port', dest='multicast_port', 
      help="multicast port")
   
   args = ap.parse_args()

   if not args.log_path.endswith("/"):
      args.log_path+="/"

   rh = logger(port=args.port, log_dir=args.log_path, 
      castgrp=args.multicast_grp, castport=args.multicast_port)

   wserver = http.server.ThreadingHTTPServer( ("",args.port), rh)
   wserver.protocol_version = "HTTP/1.1"

   try:
      wserver.serve_forever()
   except KeyboardInterrupt:
      pass
      wserver.server_close()
