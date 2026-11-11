import sys, types, importlib.util, asyncio, hashlib
from urllib.parse import urlsplit, parse_qs
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import Response

# Stubs keep this isolated test independent from the project's Telegram/Mongo services.
class FakeObjectId:
    def __init__(self, value): self.value = str(value)
    def __str__(self): return self.value
    def __repr__(self): return self.value

bson = types.ModuleType('bson'); bson.ObjectId = FakeObjectId; sys.modules['bson'] = bson

class FakeCursor:
    def __init__(self, docs): self.docs = docs
    def __aiter__(self): self._it = iter(self.docs); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration

class FakeCollection:
    def __init__(self, docs=None): self.docs = list(docs or [])
    async def create_index(self, *a, **kw): return 'ok'
    def find(self, query=None, projection=None): return FakeCursor(self.docs)
    async def find_one(self, query):
        for doc in self.docs:
            ok = True
            for k,v in query.items():
                if str(doc.get(k)) != str(v): ok=False
            if ok: return dict(doc)
        return None
    async def insert_one(self, doc):
        doc = dict(doc); doc.setdefault('_id', 'session-test'); self.docs.append(doc)
        class R: inserted_id='session-test'
        return R()
    async def update_one(self, query, update, upsert=False):
        for doc in self.docs:
            if all(str(doc.get(k)) == str(v) for k,v in query.items()):
                doc.update(update.get('$set', {})); return None
        if upsert:
            self.docs.append(dict(query, **update.get('$set', {})))
    async def delete_one(self, query):
        self.docs[:] = [d for d in self.docs if not all(str(d.get(k)) == str(v) for k,v in query.items())]
        return None

class FakeDB:
    def __init__(self, collections): self.collections=collections
    def __getitem__(self,k): return self.collections.setdefault(k, FakeCollection())

movie = {
    '_id':'movie-object-1','title':'Test Movie','title_english':'Test Movie','tmdb_id':123,
    'release_year':2024,'description':'A test movie','rating':7.5,'poster':'https://img.example/poster.jpg',
    'backdrop':'https://img.example/backdrop.jpg','runtime':'1h 40m',
    'telegram':[{'id':'encoded-movie-stream-id','name':'Test.Movie.2024.1080p.mkv','quality':'1080p','size':'1 GB'}]
}
tv = {
    '_id':'tv-object-1','title':'Test Show','tmdb_id':456,'release_year':2023,
    'description':'A test show','poster':'https://img.example/show.jpg',
    'seasons':[{'season_number':1,'episodes':[{'episode_number':1,'title':'Pilot','overview':'First episode',
      'telegram':[{'id':'encoded-episode-stream-id','name':'Test.Show.S01E01.mkv','quality':'720p','size':'500 MB'}]}]}]
}
tracking = FakeDB({'jellyfin_sessions':FakeCollection(), 'jellyfin_playback_state':FakeCollection()})
storage = FakeDB({'movie':FakeCollection([movie]), 'tv':FakeCollection([tv])})
db_obj = types.SimpleNamespace(dbs={'tracking':tracking, 'storage_1':storage})

Backend = types.ModuleType('Backend'); Backend.__path__=[]; Backend.db=db_obj; sys.modules['Backend']=Backend
config = types.ModuleType('Backend.config')
class Telegram:
    JELLYFIN_COMPAT_ENABLED=True; JELLYFIN_USERNAME='tester'; JELLYFIN_PASSWORD='very-secret';
    JELLYFIN_SERVER_ID='server-id'; JELLYFIN_SERVER_NAME='Test Telegram Server'; JELLYFIN_TOKEN_TTL_DAYS=30
config.Telegram=Telegram; sys.modules['Backend.config']=config
for name in ['Backend.fastapi','Backend.fastapi.routes','Backend.fastapi.security','Backend.helper']:
    mod=types.ModuleType(name); mod.__path__=[]; sys.modules[name]=mod
stream_routes=types.ModuleType('Backend.fastapi.routes.stream_routes')
async def stream_handler(**kwargs):
    assert kwargs['request'].path_params.get('name') == 'Test.Movie.2024.1080p.mkv'
    assert kwargs['request'].path_params.get('id') == 'encoded-movie-stream-id'
    assert kwargs['request'].headers.get('range') == 'bytes=0-1023'
    return Response('stream-ok', media_type='video/x-matroska')
stream_routes.stream_handler=stream_handler; sys.modules[stream_routes.__name__]=stream_routes
tokens=types.ModuleType('Backend.fastapi.security.tokens')
async def verify_token(token): return {'name':'source-token','limits':{},'usage':{},'subscription_expired':False,'limit_exceeded':None}
tokens.verify_token=verify_token; sys.modules[tokens.__name__]=tokens
settings=types.ModuleType('Backend.helper.settings_manager')
class Settings:
    base_url='https://test.example'; session_secret='stable-secret'
class SettingsManager:
    @staticmethod
    def current(): return Settings()
settings.SettingsManager=SettingsManager; sys.modules[settings.__name__]=settings
strm=types.ModuleType('Backend.helper.strm_generator')
async def resolve_strm_token(): return 'source-token'
strm.resolve_strm_token=resolve_strm_token; sys.modules[strm.__name__]=strm
logger=types.ModuleType('Backend.logger')
class Logger:
    def warning(self,*a): pass
logger.LOGGER=Logger(); sys.modules['Backend.logger']=logger

path=Path(__file__).resolve().parents[1] / 'Backend/fastapi/routes/jellyfin_routes.py'
spec=importlib.util.spec_from_file_location('Backend.fastapi.routes.jellyfin_routes', path)
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)

app=FastAPI(); app.include_router(mod.router)
openapi_schema=app.openapi()
assert '/System/Info/Public' in openapi_schema['paths']
assert '/Items/{item_id}/PlaybackInfo' in openapi_schema['paths']
client=TestClient(app)

r=client.get('/System/Info/Public'); assert r.status_code==200, r.text
assert r.json()['ProductName'] == 'Jellyfin Server', r.json()
lower_info=client.get('/system/info/public'); assert lower_info.status_code==200, lower_info.text
assert lower_info.json()['Id'] == r.json()['Id']
bad=client.post('/Users/AuthenticateByName', json={'Username':'tester','Pw':'wrong-password'})
assert bad.status_code==401, bad.text
r=client.post('/Users/AuthenticateByName', json={'Username':'tester','Pw':'very-secret'}, headers={'Authorization':'MediaBrowser Client="Test", Device="Phone", DeviceId="phone-1", Version="1.0"'})
assert r.status_code==200, r.text
access=r.json()['AccessToken']; auth={'Authorization':f'MediaBrowser Token="{access}", Client="Test", Device="Phone", DeviceId="phone-1", Version="1.0"'}
assert r.json()['SessionInfo']['Id'] == 'session-test'
legacy=client.post('/Users/AuthenticateByName', json={'Username':'tester','Password':hashlib.sha1(b'very-secret').hexdigest()})
assert legacy.status_code==200, legacy.text
assert client.get('/Users/Me',headers=auth).status_code==200
assert client.get('/Users/Public').json()[0]['Name']=='tester'
views=client.get('/UserViews',headers=auth); assert views.status_code==200, views.text
movies_view=views.json()['Items'][0]['Id']; shows_view=views.json()['Items'][1]['Id']
movies=client.get('/Items',params={'ParentId':movies_view},headers=auth); assert movies.status_code==200 and movies.json()['TotalRecordCount']==1, movies.text
movie_id=movies.json()['Items'][0]['Id']
movie_detail=client.get('/Items/'+movie_id,headers=auth).json()
assert movie_detail['Name']=='Test Movie'
assert movie_detail['RunTimeTicks']==100*60*10_000_000
assert mod._runtime_ticks('100 min') == 100*60*10_000_000
assert mod._runtime_ticks('1h 40m') == 100*60*10_000_000
play=client.post('/Items/'+movie_id+'/PlaybackInfo',headers=auth); assert play.status_code==200, play.text
playback_url=play.json()['MediaSources'][0]['Path']
playback_parts=urlsplit(playback_url)
playback_token=parse_qs(playback_parts.query).get('api_key',[None])[0]
assert playback_token == access and 'source-token' not in playback_url
# Native players may open the media URL without reusing the API auth header.
stream=client.get(playback_parts.path+'?'+playback_parts.query,headers={'Range':'bytes=0-1023'})
assert stream.status_code==200 and stream.text=='stream-ok', stream.text
shows=client.get('/Items',params={'ParentId':shows_view},headers=auth); assert shows.status_code==200 and shows.json()['TotalRecordCount']==1
show_id=shows.json()['Items'][0]['Id']
seasons=client.get('/Shows/'+show_id+'/Seasons',headers=auth); assert seasons.status_code==200 and seasons.json()['TotalRecordCount']==1
season_id=seasons.json()['Items'][0]['Id']
episodes=client.get('/Items',params={'ParentId':season_id},headers=auth); assert episodes.status_code==200 and episodes.json()['TotalRecordCount']==1
print('PASS: public server info (both PascalCase and lowercase paths)')
print('PASS: exact Jellyfin product identifier and public server-info payload')
print('PASS: separate username/password authentication returns access token')
print('PASS: Jellyfin SHA-1 password compatibility and session identifier')
print('PASS: authenticated user and library views')
print('PASS: movie browsing, item details, playback info and stream dispatch')
print('PASS: playback URL carries only the Jellyfin session token and preserves Range requests')
print('PASS: TV show → season → episode browsing')
print('PASS: Jellyfin-style auth header token validation')
latest=client.get('/Users/'+mod._user_id()+'/Items/Latest',headers=auth); assert latest.status_code==200, latest.text
assert client.post('/Sessions/Logout',headers=auth).status_code==204
assert client.get('/Users/Me',headers=auth).status_code==401
print('PASS: latest library route, logout and revoked-token rejection')
print('PASS: FastAPI OpenAPI schema generation and route registration')
