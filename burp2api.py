import sys
import xml.etree.ElementTree as ET
import json
import base64
import re
import os

#set output directory
directory = "output"

def process_tree(tree):
    """ Process the XML tree to reorder items, remove unwanted items, deduplicate, and clean entities. """

    root = tree.getroot()

    # Reordering items based on method
    methods_order = ["GET", "POST", "DELETE", "PUT", "PATCH"]
    items = list(root.findall("item"))
    # Sort items by method, considering the order in methods_order
    sorted_items = sorted(items, key=lambda x: methods_order.index(x.findtext("method")))
    
    # Remove all items from root and re-add them in sorted order
    root.clear()
    for item in sorted_items:
        root.append(item)
    
    # Further processing (deduplication, removal of OPTIONS, etc.)
    seen_methods = set()  
    for item in root.findall("item")[:]:
        url, method, uPath = item.findtext("url"), item.findtext("method"), item.findtext("path") 
        
        # Remove items with 'OPTIONS' method
        if method == "OPTIONS":
            root.remove(item)
            continue
        
        # Remove any roots to leave only API endpoints
        if uPath == "/":
            root.remove(item)
            continue

        # Deduplicate items based on method
        item_key = (url, method)
        if item_key in seen_methods:
            root.remove(item)
            continue
        seen_methods.add(item_key)

        # Remove unnecessary entities
        for entity in ["host", "port", "protocol", "extension", "responselength", "response", "comment", "time"]:
            element = item.find(entity)
            if element is not None:
                item.remove(element)

        # Base64 decode request
        request_element = item.find("request")
        if request_element is not None and request_element.text:
            decoded_request = base64.b64decode(request_element.text).decode("utf-8", errors="ignore")
            decoded_request = re.sub(r"Connection: close.*?\n\s*", "", decoded_request, flags=re.DOTALL)
            request_element.text = decoded_request
    
       # Take the GET params and put it into a new XML field 
        if method == "GET":
            gpath_element = item.find("path")
            if gpath_element is not None:
                path = gpath_element.text
                if '?' in path:
                   # Split the path at the question mark and update the path element
                   path, Gparam = path.split('?', 1)
                   gpath_element.text = path
                   # Create a new <param> element and add it to the <item>
                   getparam_element = ET.SubElement(item, "param")
                   getparam_element.text = Gparam

        # Take the other params (POST/PUT/ETC) and put it into a new XML field
        if method != "GET":
            # and while we're at it, remove any GET params for when devs are YOLOing because it breaks the OpenAPI format
           ppath_element = item.find("path")
           if ppath_element is not None:
                path = ppath_element.text
                if '?' in path:
                     path, Gparam = path.split('?', 1)
                     ppath_element.text = path
            # now sort the new XML params entity out
           req_element = item.find("request")
           if req_element is not None:
                req = req_element.text
                Pparam = req.split('\n')[-1]
                req_element.text = req
                # Create a new <param> element and add it to the <item>
                postparam_element = ET.SubElement(item, "param")
                postparam_element.text = Pparam
    
    return root

def convert_to_openapi(cleanedTree):
	
    """ Initalise the XML Tree """
    treeRoot = cleanedTree.findall("item")
	
    """ Convert the XML tree to OpenAPI format, with responses based on <status>. """
    openapi_dict = {
        "openapi": "3.0.0",
        "info": {
            "title": "",
            "version": "1.0.0",
            "description":"This API has been built with [Burp2API](https://swagger.io)."
        },
        "servers":{},
        #"tags": [{"name":"APIs", "description": "Go nuts"}],
        "paths": {}
    }
    
    baseURL = None
    APIname = None

    for item in treeRoot:

        url, uPath = item.findtext("url"), item.findtext("path")
        method = item.findtext("method")
        status = item.findtext("status") or "200"  # Default to 200 if status is not specified
        HTTPparams = item.findtext("param")

        if baseURL is None:
            splitURL = url.split('/')[2]
            APIname = splitURL.split(".")[0]
            baseURL = "https://" + splitURL
            openapi_dict["servers"] = [{"url": baseURL}] 

       # Set the API name
        openapi_dict["info"]["title"] = "Burp2API - " + APIname

        # Set up the path item
        path_item = openapi_dict["paths"].setdefault(uPath, {})
        
        path_item[method.lower()] = {
            "responses": {
                status: {"description": f"Response status {status}"}
            }#,
            #"tags": [ 
            #    "APIs"
           # ]
        }
        # Check if there are HTTP parameters
        if HTTPparams:
            # Split and deduplicate the parameters
            splitParams = list(set([param.split('=')[0] for param in HTTPparams.split('&')]))

            # Determine the location of the parameters based on the HTTP method
            param_location = "query" if method == "GET" else "query"  # Adjust 'body' as needed

            # Construct the parameters list
            parameters = [{"name": param, "in": param_location, "required": False, "schema": {"type": "string"}} for param in splitParams]

            # Add the parameters to the path item
            path_item[method.lower()]["parameters"] = parameters

            # Add responses to the path item
            path_item[method.lower()]["responses"] = {status: {"description": f"Response status {status}"}}
    return openapi_dict

def write_to_file(data, filename):
    """ Write data to a file. """
    with open(filename, "w") as f:
        f.write(data)
    print(f"Output saved to {filename}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python process_xml.py <input_xml_file>")
        sys.exit(1)

    #take XML input, parse it to element tree, get root element <items>, process it
    input_xml_file = sys.argv[1]
    tree = ET.parse(input_xml_file)
    treeProcessed = process_tree(tree)
    
    #creat output directory if it doesn't exist
    if not os.path.exists(directory):
        # Create the directory
        os.makedirs(directory)

    #Get filename for outputting
    full_path = r"C:\Users\Turv\Desktop\Burp2API\testfiles\testxml"
    file_name = os.path.basename(full_path)

    #output to OpenAPI
    openapi_json = json.dumps(convert_to_openapi(treeProcessed), indent=2)
    write_to_file(openapi_json, "output/" + file_name + ".json")

    #output modifed XML
    tree.write(f"output\{file_name}_modified")
    print(f"Modified XML saved to {file_name}_modified.xml")