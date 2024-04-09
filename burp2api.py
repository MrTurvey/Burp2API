import sys
import xml.etree.ElementTree as ET
import json
import base64
import re
import os
import json

# set output directory
outputDirectory = "output"

def process_tree(tree):
    # Extract root from XML, define the HTTP method order and use a set to track unique URL, method and paths.
    root = tree.getroot()
    methods_order = ["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"]
    seen_methods = set()
    new_items = []

    # Interate over the XML items, sorted by the defined HTTP method order.
    for item in sorted(root.findall("item"), key=lambda x: methods_order.index(x.findtext("method"))):
        # Extracts URL, method, and path text from the current item.
        url, method, uPath = item.findtext("url"), item.findtext("method"), item.findtext("path")
        # Creates a unique key based on URL, method, and path.
        item_key = (url, method, uPath)
        
        # Skips processing for OPTIONS methods, root path, or already seen item keys.
        if method == "OPTIONS" or uPath == "/" or item_key in seen_methods:
            continue
        
        # Marks the current item's key as seen.
        seen_methods.add(item_key)
        
        # Removes unrequired sub-elements from the XML to clean it up.
        for entity in ["host", "port", "protocol", "extension", "responselength", "response", "comment", "time"]:
            element = item.find(entity)
            if element is not None:
                item.remove(element)

        # Processes and cleans the "request" element's text, if present.
        request_element = item.find("request")
        if request_element is not None and request_element.text:
            decoded_request = base64.b64decode(request_element.text).decode("utf-8", errors="ignore")
            decoded_request = re.sub(r"Connection: close.*?\n\s*", "", decoded_request, flags=re.DOTALL)
            request_element.text = decoded_request

        # Splits the path from it's parameters and adds a new "param" element with the parameters, if they exist.
        path_element = item.find("path")
        if path_element is not None and '?' in path_element.text:
            path, params = path_element.text.split('?', 1)
            path_element.text = path
            params_element = ET.SubElement(item, "param")
            params_element.text = params
        elif method != "GET" and request_element is not None:
            params = request_element.text.split('\n')[-1]
            params_element = ET.SubElement(item, "param")
            params_element.text = params

        # Adds the cleaned and possibly modified item to the new items list.
        new_items.append(item)

    # Clears the root element
    root.clear()
    # Re-populates the root element with the cleaned and processed items.
    for item in new_items:
        root.append(item)

    # Returns the modified XML.
    return root

# For checking if the input is a valid JSON string
def is_json_param(param):
    try:
        json.loads(param)  # Try to parse the parameter as JSON
        return True
    except ValueError:
        return False


def convert_to_openapi(cleanedTree):
    # Find all elements in the XML created in the process_tree function
    items = cleanedTree.findall("item")

    # If there are no "item" elements, return an empty dictionary.
    if not items:
        return {}  # Return an empty dict or a base structure if no items found

    # Extract the "url" text from the first XML "item" and split it to get the domain part.
    first_url = items[0].findtext("url")
    splitURL = first_url.split('/')[2]
    
     # Determine the API name. If "www." or "api." is in the URL, use the next string for the name.
    api_name = splitURL.split('.')[1] if any(prefix in first_url for prefix in ["www.", "api."]) else splitURL.split('.')[0]
    # Construct the base URL using the domain.
    base_url = f"https://{splitURL}"

    # Initialize the OpenAPI specification dictionary with basic information and server URL.
    openapi_dict = {
        "openapi": "3.0.0",
        "info": {
            "title": f"Burp2API - {api_name}",
            "version": "1.0.0",
            "description": "This API has been built with [Burp2API](https://github.com/MrTurvey/Burp2API). "
                           "This is NOT a replacement for proper documentation that should stem from the official developers."
        },
        "servers": [{"url": base_url}],
        "paths": {}
    }

    # Iterate over each XML "item" to populate the "paths" in the OpenAPI spec.
    for item in items:
        # Extract relevant details from each "item".
        url, uPath = item.findtext("url"), item.findtext("path")
        method = item.findtext("method").lower()
        status = item.findtext("status") or "200"  # Default to 200 if status is not specified
        HTTPparams = item.findtext("param")

        # Prepare the path and method in the OpenAPI dictionary, initializing if not existent.
        path_item = openapi_dict["paths"].setdefault(uPath, {})
        method_item = path_item.setdefault(method, {"responses": {status: {"description": f"Response status {status}"}}})

        # Check if there are HTTP parameters
        if HTTPparams:
            parameters, requestBody = [], {"content": {}}
            for param in set(HTTPparams.split('&')):
                param_name = param.split('=')[0]
                # If the parameter is a JSON object, process it.
                if is_json_param(param_name):
                    properties_dict = {key: {"type": "string"} for key in json.loads(param_name).keys()}
                    requestBody["content"]["application/json"] = {
                        "schema": {"type": "object", "properties": properties_dict}
                    }
                else:
                    # Otherwise, treat it as a query parameter.
                    parameters.append({
                        "name": param_name,
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"}
                    })

            # If there's a requestBody defined, add it to the method item.
            if requestBody["content"]:
                method_item["requestBody"] = requestBody
            # If there are query parameters, add them to the method item
            if parameters:
                method_item["parameters"] = parameters

    # Return the constructed OpenAPI specification dictionary.
    return openapi_dict

def write_to_file(data, filename):
    """ Write data to a file. """
    with open(filename, "w") as f:
        f.write(data)
    print(f"Output saved to {filename}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python burp2api.py <input_xml_file>")
        sys.exit(1)

    # take XML input, parse it to element tree, get root element <items>, process it
    input_xml_file = sys.argv[1]
    tree = ET.parse(input_xml_file)
    treeProcessed = process_tree(tree)
    
    # create output directory if it doesn't exist
    if not os.path.exists(outputDirectory):
        # Create the directory
        os.makedirs(outputDirectory)

    # Extract just the XML file name
    file_name = os.path.basename(sys.argv[1])
    
    # output to OpenAPI JSON
    openapi_json = json.dumps(convert_to_openapi(treeProcessed), indent=2)
    write_to_file(openapi_json, outputDirectory + "/" + file_name + ".json")

    # output modifed XML
    tree.write(os.path.join(outputDirectory, f"{file_name}_modified"))

    print(f"Modified XML saved to {file_name}_modified.xml")