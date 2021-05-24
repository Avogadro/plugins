import urllib
import urllib.request

import json
import io
import base64

apiString = 'https://api.github.com/repos'
githubString = 'https://github.com'
pluginJson = []

def remove_prefix(text, prefix):
    if text.startswith(prefix):
        return text[len(prefix):]
    return text  # or whatever

def getMetaData(repoName):
    repoData = {}
    # Get name, description, updated_at
    dataUrl = apiString + repoName

    req = addAuth(dataUrl)
    response = urllib.request.urlopen(req)
    metaData = json.load(response)
    metaJson = json.dumps(metaData)
    resp = json.loads(metaJson)

    repoData['name'] = remove_prefix(resp['name'], 'avogadro-')
    repoData['repo'] = githubString + repoName
    repoData['description'] = resp['description']
    repoData['updated_at'] = resp['updated_at']
    repoData['branch'] = resp['default_branch']

    # See if there is a release and get the latest one
    releasesUrl = apiString + repoName + '/releases'
    req = addAuth(releasesUrl)
    response = urllib.request.urlopen(req)
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
        repoData['zipball_url'] = apiString + repoName + '/zipball/' + repoData['branch']

    # parse the repo's plugin.json
    pluginJsonUrl = apiString + repoName + '/contents/plugin.json'
    print(pluginJsonUrl)
    req = addAuth(pluginJsonUrl)
    try:
        response = urllib.request.urlopen(req)
        data = json.load(response)
        json_str = json.dumps(data)
        resp = json.loads(json_str)
        contentJson = resp['content']
        contentJson = base64.b64decode(contentJson)

        try:
            result = json.loads(contentJson)
            # fix some common errors
            if 'type' in result:
                type = result['type']
            if type == 'input' or type == 'generators':
                type = 'inputGenerators'
            elif type == 'formats':
                type = 'formatScripts'
            repoData['type'] = type

        except json.decoder.JSONDecodeError:
            repoData['type'] = 'other'
    except urllib.error.HTTPError:
        repoData['type'] = 'other'

    pluginJson.append(repoData)
    return


def addAuth(url):
    dataUrl = urllib.request.Request(url)
    base64string = 'ZXRwMTI6cXdlcnR5Njc='
    dataUrl.add_header("Authorization", "Basic %s" % base64string)
    return dataUrl

with io.open('repositories.txt', 'r') as repoFile:
    repoList = [line.rstrip('\n') for line in repoFile]

for repo in repoList:
    repoName = repo[18:]
    getMetaData(repoName)

with io.open('masterPlugin.json', 'w', encoding='utf-8') as f:
    f.write(json.dumps(pluginJson, ensure_ascii=False))
