# crbl_logger

crbl_logger is a text extraction service that provides a simple REST API to view and perform basic search on text files in a network. While it is named "logger", any text based files can be searched and results provided to the user. Each server should have one log service installed. The log service is started with configuration parameters, including which directory to monitor. Files are memory mapped and thus allows for arbitrary large files to be efficiently processed without "disk thrashing". Simple "tail N" line operations without search will be near instant regardless of file size.

When multiple log services are installed through the network, the services will discover each other via UDP multicasting. This will allow dynamic routing via URL redirects of log file requests.

## Installation
Python 3.8.10 or higher is required, no other external libraries are required.

## Starting the service
```
usage: log.py [-h] [-p PORT] [-d LOG_FOLDER_PATH] [-m multicast IP] [-g multicast port]

log file service

optional arguments:
  -h, --help          show this help message and exit
  -p PORT             port (default is 7777)
  -d LOG_FOLDER_PATH  main log folder (default is /var/log)
  -m multicast IP     multicast group (default is 239.0.1.5)
  -g multicast port   multicast port (default is 8888)
```
## API
Two main REST calls are provided. These calls use URL paths and parameters.

### /log
/log provides the main functionality of interest by extracting the tail N lines of a file and optionally filtering.

**parameters:**

- fn=[file to scrape]:REQUIRED - This is the file we want to process. Relative paths to the main log folder are valid. So if the main log folder is `/var/log`, then fn could have `apt/term.log`

- n=[num]:OPTIONAL (default=-1) - This is the tail number of lines to return/search. The default of -1 means return/search the entire file.

- ftr=[search string]:OPTIONAL - The search string has the following format: `OPERATOR|TERM1+TERM2+...+TERM_N`, where OPERATOR=[and|or|{NULL}]. The operator may be empty and the search string simply contain a list of terms: `TERM1+TERM2+...+TERM_N` and the operator is an implied AND, meaning in the absence of an explicit operator, all terms are required (AND-ed) together. Multiple `ftr` instances may be provided, for example `ftr=and|los+angeles&ftr=and|06:22:52+PDT+2022&ftr=or|fatal+warn+error`

- r=[(t)rue|(f)alse]:OPTIONAL (default=(f)alse) - This is flag, short for "route", will attempt to HTTP redirect a log file to appropriate log service that contains it. So if file `dpkg.log.1` resides at 192.168.5.1, we can query any log service in the network with `r=t` and the client request will be redirected if possible. NOTE: This is a very simple routing service and can only handle 0 level depth from the main log folder. In other words, the log services do not recursively scan subfolders. While the `fn` parameter can handle paths, the `r(outing)` parameter effectively effectively nullifies paths. This also implies that for routing enabled, files must be unique across the network. While these are significant shortcomings, version 0.01 is intended as a simple prototype. 

examples:
``` 
    # boolean searches last 10000 lines of /var/log/apt/term.log for occurrences of "(error or warn or fatal) and (new and york)"
    curl http://localhost:7777/log?fn=apt/term.log&ftr=or|error+warn+fatal&ftr=and|new+york&n=10000 
    
    # searches across the network of log services for file, term.log, and performs implicit AND of terms "error, warn, fatal"
    # Note the redirect flag, `-L` on curl is required to perform the redirect
    curl -L http://192.68.5.2:7777/log?fn=term.log&ftrerror+warn+fatal&r=t     
```

### /ls
/ls provides a means to list the directory contents of any or every log service in the network. Unfortunately, due to the simple nature of this release, files and folders are not currently distinguished in the returned list. /ls can provide a local listing of any given log service or it can query all the other log services in the network to provide listings of all the files across them. A path can also be provided to list subfolders of a particular local service.

** parameters **

- fn=[path]:OPTIONAL - relative path from the main log folder to list. This effectively allows listing of subfolder contents.

- g[(t)rue|(f)alse]:OPTIONAL (default=(f)alse) - short for "global", indicates whether to list only the local contents of the log service residing at the given host/port or to list all the files across all the log services. NOTE, if `g=true`, then the `fn=path` parameter is ignored.

- Accept: text/plain: OPTIONAL (default=plain/text) - This is an HTTP header indicating whether to return the listings as plain text
- Accept: application/json: - This content-type will return the /ls listing in json format.

examples:
``` 
     # lists all the files at shazam252:/var/log
     curl http://shazam252:7777/ls
     
     # lists all the files at 192.168.1.2:/var/log/apt and returns in json format
     curl -H "Accept: application/json" http://192.168.1.2:7777/ls?fn=apt
     
     # lists all the files under the respective log folders of all log services in the network and returns plain text results
     curl -H "Accept: text/plain" http://localhost:7777/ls?g=t
