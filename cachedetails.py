import urllib2
import json
import io
import base64

apiString = 'https://api.github.com/repos'
pluginJson = []


def getMetaData(repoName):
    repoData = {}
    # Get name, description, updated_at
    dataUrl = apiString + repoName

    req = addAuth(dataUrl)
    response = urllib2.urlopen(req)
    metaData = json.load(response)
    metaJson = json.dumps(metaData)
    resp = json.loads(metaJson)

    repoData['name'] = resp['name']
    repoData['description'] = resp['description']
    repoData['updated_at'] = resp['updated_at']

    # See if there is a release and get the latest one
    releasesUrl = apiString + repoName + '/releases'
    req = addAuth(releasesUrl)
    response = urllib2.urlopen(req)
    data = json.load(response)
    json_str = json.dumps(data)
    resp = json.loads(json_str)

    if len(resp) > 0:
        repoData['has_release'] = True
        repoData['release_version'] = resp[0]['tag_name']
        repoData['zipball_url'] = resp[0]['zipball_url']
    else:
        repoData['has_release'] = False
        repoData['release_version'] = 'N/A'
        repoData['zipball_url'] = apiString + repoName + '/zipball/master'
    pluginJson.append(repoData)
    return

    # parse the repo's plugin.json
    pluginJsonUrl = apiString + repoName + '/contents/plugin.json'
    req = addAuth(pluginJsonUrl)
    response = urllib2.urlopen(req)
    data = json.load(response)
    json_str = json.dumps(data)
    resp = json.loads(json_str)
    contentJson = resp['content']
    contentJson = base64.b64decode(contentJson)

    result = json.loads(contentJson)
    print contentJson
    if 'type' in resp:
        repoData['type'] = resp['type']
    else:
        repoData['type'] = 'other'


def addAuth(url):
    dataUrl = urllib2.Request(url)
    base64string = 'ZXRwMTI6cXdlcnR5Njc='
    dataUrl.add_header("Authorization", "Basic %s" % base64string)
    return dataUrl

with io.open('repoFile.txt', 'r') as repoFile:
    repoList = [line.rstrip('\n') for line in repoFile]

for repo in repoList:
    repoName = repo[18:]
    getMetaData(repoName)

with io.open('masterPlugin.json', 'w', encoding='utf-8') as f:
    f.write(unicode(json.dumps(pluginJson, ensure_ascii=False)))
