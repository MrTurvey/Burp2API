# Burp2API - Turn Burp Suite Projects Into JSON
## What is this project? 

Burp2API converts your Burp Suite project into a JSON for usage with POSTMAN or SWAGGER editor.

There are currently two purposes for the creation of this tool:
- To provide clients with documentation for their own API (Anyone that does API testing knows the struggles)
- To save a history of the endpoints you used in previous testing which can be used for future testing.  

This project is maintained by [TurvSec](https://twitter.com/TurvSec)

## Installation
Simply git clone and you're away:
```
git clone https://github.com/MrTurvey/Burp2API.git
cd Burp2API
python Burp2API <BurpXML>
```

## Usage
With your BurpSuite project loaded:
- Click on the target tab, if you're not already there
- Right click the target you want to export
- Click "Save selected items"
- Ensure the "Base64-encode" checkbox is checked
- Save as burp_output.xml, or whatever you want

Now you have a Burp Suite output, you can use Burp2API: 

```
python Burp2API burp_output.xml
```
The output will be 

- filename.json
- filename.xml

You can view the JSON file with Swagger (link below) or you can import it into Postman using its import button.

https://editor.swagger.io

## Limitations 

The tool is fresh, there are probably a lot of things that need fixing.