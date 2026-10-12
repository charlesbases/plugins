"""四个批准selector中的实际格式回归；候选预期独立于validator业务函数。"""
import base64
import copy
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from investment.contracts import ContractError, SPEC, artifact, canonical, known, now, unknown, na
from investment.chains.news.contracts import NewsResolver, NewsSchemas, REVIEW_USES, SOURCES, news_dataset
from investment.runtime.paths import test_parent, validate_root
from tests import harness

PLUGIN = Path(__file__).resolve().parents[1]
TEXT = 'The publisher records a policy release with its original wording.'


def identity(prefix,*values):
    return prefix+'-'+hashlib.sha256(canonical(list(values))).hexdigest()[:40]


def gap(rule,pointer,reason):
    return {'rule_id':rule,'field_or_object':pointer,'reason':reason,'owner':'source',
            'required_evidence':['source_bytes'],'affected_uses':REVIEW_USES}


class ValidatorCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.root = validate_root(test_parent()/('sc02-contracts-'+uuid.uuid4().hex),fixture_mode=True)
        self.root.mkdir();self.schemas = NewsSchemas();self.responses = {};self.doc_paths = set()
        owner = self
        class Publisher(BaseHTTPRequestHandler):
            def do_GET(self):
                status,body,mime = owner.responses[self.path]
                encoded = gzip.compress(body,mtime=0) if self.path in owner.doc_paths else body
                self.send_response(status);self.send_header('Content-Type',mime)
                self.send_header('Content-Length',str(len(encoded)))
                if encoded != body:self.send_header('Content-Encoding','gzip')
                self.end_headers();self.wfile.write(encoded)
            def log_message(self,*args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Publisher)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base = 'http://127.0.0.1:'+str(self.server.server_port)
        folder,incoming = harness.build_input(self.root,'calibration-account',[])
        upstream = harness.invoke(self.root,folder,incoming)
        (self.root/'sc01.stdout.json').write_text(upstream.stdout,encoding='utf-8')
        (self.root/'sc01.stderr.log').write_text(upstream.stderr,encoding='utf-8')
        self.assertEqual(upstream.returncode,0,upstream.stderr)
        result = json.loads(upstream.stdout)
        resolver = NewsResolver(self.root,self.schemas,mode='test',network_origin='simulated')
        seal = resolver.resolve(result['sealed_output_ref'],'investment.sealed_output')
        self.account_ref = resolver.resolve(seal['candidate_output_ref'],'investment.candidate_output')['payload_ref']
        self.run_id = incoming['run_id']
        self.day = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
        print('SC02 calibration artifacts: '+self.root.as_posix(),file=sys.stderr)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def put(self,name,value,sid,attempt=True):
        directory = self.attempt if attempt else self.folder
        (directory/name).write_bytes(canonical(value))
        return artifact(canonical(value),sid,self.prefix+('attempt-01/' if attempt else '')+name)

    def request(self,sid,path,purpose,calibration=False):
        start = now()
        with httpx.Client(timeout=5,follow_redirects=False,headers={'Accept-Encoding':'gzip, identity'}) as client:
            with client.stream('GET',self.base+path) as response:
                raw = b''.join(response.iter_raw());status = response.status_code
                headers,url = response.headers.raw,str(response.request.url);request_headers = response.request.headers.raw
        end = now();sequence = len(self.captures)+1
        capture = {'capture_id':'cap-'+str(sequence),'request_id':identity('req',self.run_id,self.invocation_id,1,sid),
            'source_id':sid,'sequence_no':sequence,'parent_capture_id':na(),'purpose':purpose,'method':'GET',
            'requested_url':url,'final_url':url,'request_headers':[{'name':n.decode('latin1'),'value':v.decode('latin1')} for n,v in request_headers],
            'response_headers':[{'name':n.decode('latin1'),'value':v.decode('latin1')} for n,v in headers],
            'http_status':known(status),'redirect_target':na(),'request_started_at':start,'response_finished_at':end,
            'capture_state':'complete','origin':'synthetic','response_bytes_base64':base64.b64encode(raw).decode(),
            'response_sha256':hashlib.sha256(raw).hexdigest(),'response_byte_size':len(raw),
            'error':known({'code':'http_status','message':'HTTP '+str(status)}) if status >= 400 else na()}
        if calibration:
            directory = self.root/'profile-calibration';directory.mkdir(exist_ok=True)
            (directory/(self.invocation_id+'-'+sid+'.json')).write_bytes(canonical(capture))
            return capture
        reference = self.put('capture-'+capture['capture_id']+'.json',capture,'investment.news_capture')
        self.captures.append((reference,capture));return reference,capture

    def start_variant(self):
        self.invocation_id = 'news-'+uuid.uuid4().hex
        self.folder = self.root/'runs/sc02'/self.day/self.invocation_id;self.attempt = self.folder/'attempt-01'
        self.attempt.mkdir(parents=True);self.plugins = self.root/('plugins-'+self.invocation_id);self.plugins.mkdir()
        self.prefix = f'investment-run://sc02/{self.day}/{self.invocation_id}/'
        self.captures = [];self.responses = {};self.doc_paths = set();self.specs = {};self.profiles = {}

    def prepare(self,kind,video,publication,conflict,unfetched):
        self.point = '2026-10-18T12:00:00Z' if publication else (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(timespec='seconds').replace('+00:00','Z')
        endpoint = '/source_a/one';title = 'Release One';native = 'text/html';body = TEXT
        raw = ('<html><body><h1>Release One</h1><time datetime="'+self.point+'"></time><div id="body">'+TEXT+'</div></body></html>').encode()
        profile = {'source_id':'source_a','profile_version':'v1','normalization':'sc02-text-v1','charset':'utf-8','calibration_state':'calibrated',
            'entry_follow_rules':[],'listing_rules':[{'url_pattern':re.escape(self.base+'/source_a/list'),'format':'html','items_locator':'//body/a','url_locator':'@href','title_locator':'.','time_locator':'','next_locator':'','link_prefix':'','material_kind':'official_release','time_role':'published','exhaustion_policy':'latest_only'}],
            'document_rules':[{'url_pattern':re.escape(self.base+endpoint),'format':'html','title_locator':'//h1','body_locator':'//*[@id="body"]','time_locator':'//time/@datetime','time_format':'rfc3339','timezone':unknown('offset_in_source'),'time_role':'published','material_kind':'official_release'}],
            'whole_window_proof':unknown('window_completeness_not_evidenced'),'limitations':['Finite local publisher; synthetic format calibration only.']}
        spec = {'endpoint':endpoint,'title':title,'text':body,'native':native,'format':'html_text','literal':known(self.point),
                'at':self.point,'reason':None,'membership':'within','basis':'body'}
        label = title;locator = '/html/body/a'
        if kind in ('json','xml') or video:
            source = 'usgs' if kind == 'json' else 'sec_edgar' if kind == 'xml' else 'xinhua'
            profile = json.loads((SOURCES/source/'extraction.json').read_bytes())
            profile.update(source_id='source_a',entry_follow_rules=[],limitations=['Actual observed source format, local synthetic publisher.'])
            profile['listing_rules'][0]['url_pattern'] = re.escape(self.base+'/source_a/list')
        if kind == 'json':
            epoch_ms = int(datetime.fromisoformat(self.point.replace('Z','+00:00')).timestamp())*1000
            record = {'type':'Feature','id':'local-event','properties':{'title':'M 1.4 - Local event','time':epoch_ms,'mag':1.4},'geometry':{'type':'Point','coordinates':[1.1,2.2,3]}}
            raw = json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
            spec.update(title=record['properties']['title'],text=raw.decode(),native='application/geo+json',format='json_record',literal=known(str(epoch_ms)))
            feed_record = copy.deepcopy(record);feed_record['properties']['detail'] = self.base+endpoint
            listing = json.dumps({'type':'FeatureCollection','features':[feed_record]},sort_keys=True,separators=(',',':')).encode()
            label = spec['title'];locator = '/features/0'
            listed_time = known(str(epoch_ms))
        elif kind == 'xml':
            endpoint = '/source_a/one.xml'
            raw = b'<ownershipDocument><documentType>4</documentType><periodOfReport>2026-10-16</periodOfReport></ownershipDocument>'
            spec.update(endpoint=endpoint,title='4',text=raw.decode(),native='application/xml',format='xml_text',literal=unknown('source_time_missing'),at=None,reason='source_time_missing',membership='uncertain')
            listing = ('<html><body><table class="tableFile"><tr><td><a href="'+endpoint+'">form4.xml</a></td></tr></table></body></html>').encode()
            label = 'form4.xml';locator = '/html/body/table/tr/td/a';listed_time = unknown('listing_time_missing')
        elif video:
            endpoint = '/source_a/one/c.html'
            raw = ('<html><body><h1>Release One</h1><div class="time">2026 10 / 19 11:50:31</div><div id="detail"><span id="detailContent"><p>'+TEXT+'</p></span><div id="articleEdit">【责任编辑:编辑】</div></div></body></html>').encode()
            spec.update(endpoint=endpoint,literal=known('2026 10 / 19 11:50:31'),at=None,reason='source_timezone_missing',membership='uncertain')
            profile['listing_rules'][0]['exhaustion_policy'] = 'latest_only'
            video_path = '/source_a/video/c.html'
            video_raw = '<html><body><h1>Video release</h1><div class="time">2026 10 / 19 11:50:31</div><div id="detail"><span id="detailContent"><span class="pageVideo" video_src="/video.mp4"></span><p>编导：唐子晔</p><p>新华社音视频部制作</p></span><div id="articleEdit">【责任编辑:焦鹏】</div></div></body></html>'.encode()
            self.responses[video_path] = (200,video_raw,'text/html; charset=utf-8');self.doc_paths.add(video_path)
            self.video = {'endpoint':video_path,'raw':video_raw,'title':'Video release','text':'编导：唐子晔 新华社音视频部制作 【责任编辑:焦鹏】'}
            listing = ('<html><body><a href="'+endpoint+'">Release One</a><a href="'+video_path+'">Video release</a></body></html>').encode()
            locator = '/html/body/a[1]';listed_time = unknown('listing_time_missing')
        else:
            listing = ('<html><body><a href="'+endpoint+'">Release One</a>'+('<a href="/source_a/two">Release Two</a>' if unfetched else '')+'</body></html>').encode()
            locator += '[1]' if unfetched else '';listed_time = unknown('listing_time_missing')
        profile['document_rules'][0]['url_pattern'] = re.escape(self.base+endpoint) if not video else re.escape(self.base+'/source_a/')+r'(?:one|video)/c\.html'
        spec['raw'] = raw;self.specs['source_a'] = spec;self.profiles['source_a'] = profile
        self.responses[endpoint] = (200,raw,spec['native']);self.doc_paths.add(endpoint)
        self.responses['/source_a/specimen'] = (200,raw,spec['native'])
        self.responses['/source_a/list'] = (200,listing,'application/geo+json' if kind == 'json' else 'text/html; charset=utf-8')
        self.opportunity_specs = [('source_a',endpoint,label,listed_time,[locator],'captured')]
        if unfetched:self.opportunity_specs.append(('source_a','/source_a/two','Release Two',unknown('listing_time_missing'),['/html/body/a[2]'],'pending_fetch'))
        if video:self.opportunity_specs.append(('source_a',self.video['endpoint'],'Video release',unknown('listing_time_missing'),['/html/body/a[2]'],'pending_parse'))
        if publication:self.prepare_publication(publication,conflict)

    def prepare_publication(self,source_format,conflict):
        source = 'fed' if source_format == 'rss' else 'who'
        profile = json.loads((SOURCES/source/'extraction.json').read_bytes())
        profile.update(source_id='source_b',entry_follow_rules=[],limitations=['Exact observed RSS/API publication format, local synthetic publisher.'])
        profile['listing_rules'][0]['url_pattern'] = re.escape(self.base+'/source_b/list')
        profile['document_rules'][0]['url_pattern'] = re.escape(self.base+'/source_b/one')
        endpoint = '/source_b/one';text = 'Earlier publisher release with its original wording.'
        if source_format == 'rss':
            literal = 'October 16, 2026';at = '2026-10-16T15:00:00Z';pub = 'Fri, 16 Oct 2026 15:00:00 GMT'
            items = '<item><title>Previous release</title><link>'+self.base+endpoint+'</link><pubDate>'+pub+'</pubDate></item>'
            if conflict:items += '<item><title>Previous release</title><link>'+self.base+endpoint+'</link><pubDate>Sun, 18 Oct 2026 12:00:00 GMT</pubDate></item>'
            listing = ('<?xml version="1.0"?><rss version="2.0"><channel>'+items+'</channel></rss>').encode()
            raw = ('<html><body><h3 class="title">Previous release</h3><p class="article__time">'+literal+'</p><div id="article">'+text+'</div></body></html>').encode()
            locators = ['/rss/channel/item[1]','/rss/channel/item[2]'] if conflict else ['/rss/channel/item']
            mime = 'application/rss+xml'
        else:
            literal = '15 October 2026';at = '2026-10-15T16:00:00Z';pub = at
            listing = json.dumps({'value':[{'Title':'Previous release','ItemDefaultUrl':'/one','PublicationDateAndTime':at,
                'EmergencyEvent':{'PublicationDate':'2021-04-14T12:51:20Z','LastModified':'2023-01-04T09:04:47Z'}}]},sort_keys=True,separators=(',',':')).encode()
            raw = ('<html><body><h1 class="don-title">Previous release</h1><div class="date">'+literal+'</div><article class="sf-detail-body-wrapper">'+text+'</article></body></html>').encode()
            profile['listing_rules'][0]['link_prefix'] = self.base+'/source_b'
            locators = ['/value/0'];mime = 'application/json'
        self.specs['source_b'] = {'raw':raw,'endpoint':endpoint,'title':'Previous release','text':text,'native':'text/html',
            'format':'html_text','literal':known(literal),'at':None if conflict else at,
            'reason':'conflicting_publication_evidence' if conflict else None,'membership':'uncertain' if conflict else 'publication',
            'basis':'listing'}
        self.profiles['source_b'] = profile
        self.responses['/source_b/list'] = (200,listing,mime);self.responses['/source_b/specimen'] = (200,raw,'text/html')
        self.responses[endpoint] = (200,raw,'text/html');self.doc_paths.add(endpoint)
        self.opportunity_specs.append(('source_b',endpoint,'Previous release',known(pub),locators,'publication'))

    def plugin(self,sid):
        capture = self.request(sid,'/'+sid+'/specimen','document',calibration=True)
        profile = self.profiles[sid]
        kind = profile['document_rules'][0]['material_kind']
        manifest = {'source_id':sid,'plugin_version':'v1','publisher_id':sid,'publisher_name':'Local synthetic publisher',
            'entrypoints':[self.base+'/'+sid+'/list'],'allowed_hosts':['127.0.0.1'],'material_kinds':[kind],
            'interface_version':'sc02.collect.v1','module':'collect.py','callable':'collect','calibration_state':'calibrated',
            'profile_sha256':hashlib.sha256(canonical(profile)).hexdigest(),'evidence':[{'url':capture['final_url'],
                'captured_at':capture['response_finished_at'],'capture_state':'complete','http_status':known(200),
                'response_sha256':capture['response_sha256'],'response_byte_size':capture['response_byte_size'],
                'role':'material','observation':'Actual synthetic publisher specimen captured before invocation.'}],
            'limitations':profile['limitations']}
        folder = self.plugins/sid;folder.mkdir()
        (folder/'manifest.json').write_bytes(canonical(manifest));(folder/'extraction.json').write_bytes(canonical(profile))
        return {'source_id':sid,'manifest_ref':known(self.put('plugin-'+sid+'.json',manifest,'investment.news_manifest',False)),
                'profile_ref':known(self.put('profile-'+sid+'.json',profile,'investment.news_profile',False))}

    def expected_document(self,sid,raw_ref,capture,listing_ref):
        spec = self.specs[sid];rule = self.profiles[sid]['document_rules'][0]
        profile_ref = next(b for b in self.bindings if b['source_id'] == sid)['profile_ref']['value']
        body = {'source_id':sid,'capture_ref':raw_ref,'profile_ref':profile_ref,'format':spec['format'],
            'title':spec['title'],'text':spec['text'],'source_time':spec['literal'],'title_locator':rule['title_locator'],
            'body_locator':rule['body_locator'],'time_locator':rule['time_locator'],'native_media_type':spec['native']}
        body_ref = self.put('body-'+capture['capture_id']+'.json',body,'investment.news_body')
        if spec['at'] is None:time = unknown(spec['reason'])
        else:
            bound = datetime.fromisoformat(spec['at'].replace('Z','+00:00')).isoformat(timespec='microseconds').replace('+00:00','Z')
            time = known({'lower_bound':bound,'upper_bound':bound,'precision':'instant','timezone':'UTC',
                          'basis_ref':listing_ref if spec['basis'] == 'listing' else raw_ref})
        document = {'document_id':identity('doc',sid,capture['final_url']),'version_id':'ver-'+hashlib.sha256(spec['raw']).hexdigest(),
            'publisher_id':sid,'source_id':sid,'title':spec['title'],'material_kind':rule['material_kind'],
            'requested_url':capture['requested_url'],'final_url':capture['final_url'],'raw_ref':raw_ref,'body_ref':body_ref,
            'published_time':time if rule['time_role'] == 'published' else unknown('not_publication_time'),
            'window_time':time,'window_time_role':rule['time_role'],'window_membership':spec['membership'],
            'available_time':unknown('no_availability_by_cutoff'),'retrieved_at':capture['response_finished_at'],
            'availability_basis':'unknown_before_cutoff','capture_status':'complete'}
        return body,document

    def build_case(self,*,failing=False,unfetched=False,kind='html',video=False,publication=None,outside=False,conflict=False):
        self.start_variant();self.prepare(kind,video,publication,conflict,unfetched)
        if failing:
            self.profiles['source_b'] = copy.deepcopy(self.profiles['source_a'])
            self.profiles['source_b']['source_id'] = 'source_b'
            self.profiles['source_b']['listing_rules'][0]['url_pattern'] = re.escape(self.base+'/source_b/list')
            self.profiles['source_b']['document_rules'][0]['url_pattern'] = re.escape(self.base+'/source_b/one')
            self.responses['/source_b/specimen'] = (200,self.specs['source_a']['raw'],'text/html')
            self.responses['/source_b/list'] = (503,b'Service unavailable','text/plain')
        sources = sorted(self.profiles)
        self.bindings = [self.plugin(sid) for sid in sources]
        cutoff = now();start = '2026-10-17T00:00:00Z' if outside else '2026-10-15T00:00:00Z' if publication else (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds').replace('+00:00','Z')
        end = '2026-10-19T00:00:00Z' if outside else cutoff
        roles = [self.put('clock.json',{'clock_id':'clock','decision_at':cutoff,'information_cutoff_at':cutoff},'investment.decision_clock',False),
            self.put('window.json',{'window_id':'window','start_at':start,'end_at':end},'investment.news_window',False),
            self.put('scope.json',{'scope_id':'scope','entries':[{'source_id':sid,'entrypoints':[self.base+'/'+sid+'/list'],'material_kinds':[self.profiles[sid]['document_rules'][0]['material_kind']]} for sid in sources]},'investment.source_scope',False)]
        limits = {'global_concurrency':4,'per_source_concurrency':2,'timeout_ms':5000,'response_byte_limit':1048576,
                  'decoded_byte_limit':2097152,'request_budget':20,'page_budget':10,'document_budget':10,'redirect_budget':3}
        roles.append(self.put('query.json',{'protocol_id':'query','discovery_policy':'configured_entries','limits':limits},'investment.query_protocol',False));roles.append(self.account_ref)
        params = self.put('parameters.json',{'parameter_set_id':'parameters','mode':'test','network_origin':'simulated',
            'plugins_dir':self.plugins.as_posix(),'user_agent':'SC02 calibration publisher'},'investment.sc02_parameters',False)
        role_names = ['decision_clock_ref','news_window_ref','source_scope_ref','query_protocol_ref','account_risk_objects_ref']
        self.incoming = {'contract_id':'investment.stage_input','contract_version':1,'run_id':self.run_id,'stage_id':'SC02',
            'invocation_id':self.invocation_id,'decision_at':cutoff,'information_cutoff_at':cutoff,'scope_ref':roles[2],
            'input_refs':[{'role':n,'ref':r} for n,r in zip(role_names,roles)],'parameters_ref':params}
        self.input_ref = self.put('input.json',self.incoming,'investment.stage_input',False);self.started = now()
        self.listings = {};self.material_records = {};self.expected_bodies = {};self.expected_documents = {}
        for sid in sources:
            self.listings[sid] = self.request(sid,'/'+sid+'/list','entry')
            if sid in self.specs:
                self.material_records[sid] = self.request(sid,self.specs[sid]['endpoint'],'data' if kind == 'json' and sid == 'source_a' else 'document')
                if sid == 'source_b':self.specs[sid]['membership'] = 'uncertain' if conflict else 'outside' if outside else 'within'
                raw_ref,capture = self.material_records[sid]
                body,document = self.expected_document(sid,raw_ref,capture,self.listings[sid][0])
                self.expected_bodies[sid],self.expected_documents[sid] = body,document
        self.video_record = self.request('source_a',self.video['endpoint'],'document') if video else None
        documents = [d for d in self.expected_documents.values() if d['window_membership'] != 'outside']
        opportunities = []
        for sid,path,title,listed_time,locators,disposition in self.opportunity_specs:
            url = self.base+path;document = self.expected_documents.get(sid)
            if disposition == 'publication':disposition = 'excluded_outside_window' if outside and not conflict else 'captured'
            opportunities.append({'opportunity_id':identity('opp',sid,url),'source_id':sid,'url':url,
                'material_kind':self.profiles[sid]['listing_rules'][0]['material_kind'],
                'discovered_in':[{'capture_ref':self.listings[sid][0],'locator':p} for p in locators],
                'listed_title':known(title),'listed_time':listed_time,'disposition':disposition,
                'document_ids':[document['document_id']] if disposition == 'captured' else [],'reason':'actual_publisher_listing'})
        opportunities.sort(key=lambda o:o['opportunity_id'])
        pending = [{'pending_id':identity('pending',o['opportunity_id']),'source_id':o['source_id'],'opportunity_id':o['opportunity_id'],
            'reason_code':o['disposition'],'required_evidence':'supported_layout' if o['disposition'] == 'pending_parse' else 'source_response',
            'affected_uses':REVIEW_USES} for o in opportunities if o['disposition'] in ('pending_fetch','pending_parse')]
        self.rows = {'documents':sorted(documents,key=lambda d:(d['document_id'],d['version_id'])),
            'opportunity_inventory':opportunities,'pending_items':sorted(pending,key=lambda p:p['pending_id']),
            'extraction_spec':[dict(b,calibration_state='calibrated') for b in self.bindings],
            'retrieval_attempts':sorted([{k:c[k] for k in ('capture_id','request_id','source_id','sequence_no','purpose','requested_url',
                'final_url','request_started_at','response_finished_at','capture_state','origin')}|{'capture_ref':r} for r,c in self.captures],key=lambda c:c['capture_id'])}
        self.rows['coverage'] = []
        for sid in sources:
            captures = [c for _,c in self.captures if c['source_id'] == sid];docs = [d for d in documents if d['source_id'] == sid]
            errors = []
            if failing and sid == 'source_b':errors.append({'source_id':sid,'code':'request_failed','capture_id':known(captures[0]['capture_id']),'message':'HTTP 503'})
            if video and sid == 'source_a':errors.append({'source_id':sid,'code':'layout_unknown','capture_id':known(self.video_record[1]['capture_id']),'message':'layout_unknown'})
            errors.append({'source_id':sid,'code':'coverage_unknown','capture_id':unknown('source_scope_gap'),'message':'whole_window_not_proven'})
            self.rows['coverage'].append({'source_id':sid,'status':'partial' if docs else 'failed',
                'listing_state':'error' if failing and sid == 'source_b' else 'exhausted_exposed' if kind == 'xml' and sid == 'source_a' else 'unknown',
                'window_coverage':'unknown','configured_entrypoints':[self.base+'/'+sid+'/list'],'visited_entrypoints':[self.base+'/'+sid+'/list'],
                'capture_ids':sorted(c['capture_id'] for c in captures),
                'opportunity_ids':sorted(o['opportunity_id'] for o in opportunities if o['source_id'] == sid),
                'document_ids':sorted({d['document_id'] for d in docs}),'pending_ids':sorted(p['pending_id'] for p in pending if p['source_id'] == sid),
                'errors':errors,'reason':'whole_window_not_proven'})
        self.refresh_gaps()
        self.log_ref = self.put('capture-log.json',{'request_id':identity('batch',self.run_id,self.invocation_id,1),
            'run_id':self.run_id,'invocation_id':self.invocation_id,'attempt_no':1,'state':'frozen','captures':[r for r,_ in self.captures],'closed_at':now()},'investment.news_capture_log')
        self.method_ref = artifact((SPEC/'method-sc02.json').read_bytes(),'investment.sc02_method','investment-spec://v1/method-sc02.json')
        self.save_candidate()

    def refresh_gaps(self):
        self.gaps = [gap('N10','/coverage/'+r['source_id'],'whole_window_not_proven') for r in self.rows['coverage']]
        for d in self.rows['documents']:
            if d['window_membership'] == 'uncertain':
                reason = 'conflicting_publication_evidence' if d['window_time']['reason'] == 'conflicting_publication_evidence' else 'window_membership_uncertain'
                self.gaps.append(gap('N07','/documents/'+d['document_id']+'/window_time',reason))
            self.gaps.append(gap('N07','/documents/'+d['document_id']+'/available_time','no_availability_by_cutoff'))
        self.gaps.sort(key=lambda g:(g['rule_id'],g['field_or_object']))

    def save_candidate(self):
        names = ['documents','retrieval_attempts','opportunity_inventory','pending_items','extraction_spec','coverage']
        references = []
        for name in names:
            value = {'rows':self.rows[name]};self.put(name+'.json',value,'investment.news_'+name)
            references.append(news_dataset(value,'investment.news_'+name,self.prefix+'attempt-01/'+name+'.json',self.schemas))
        corpus = {'corpus_id':'corpus-'+self.input_ref['sha256'][:32],'window_ref':self.incoming['input_refs'][1]['ref'],
                  'source_scope_ref':self.incoming['scope_ref']}|{n+'_ref':r for n,r in zip(names,references)}
        payload = self.put('corpus.json',corpus,'investment.evidence_corpus')
        candidate = {'contract_id':'investment.candidate_output','contract_version':1,'run_id':self.run_id,'stage_id':'SC02',
            'output_id':'output','created_at':now(),'processing_state':'completed','input_refs':self.incoming['input_refs'],
            'scope_ref':self.incoming['scope_ref'],'method_ref':self.method_ref,'payload_ref':payload,'datasets':references,
            'evidence_refs':[self.log_ref]+[r for r,_ in self.captures],'gaps':self.gaps}
        self.candidate_ref = self.put('candidate.json',candidate,'investment.candidate_output')
        invocation = {'run_id':self.run_id,'invocation_id':self.invocation_id,'description':'Independent validator calibration',
            'frozen_input_ref':self.input_ref,'parameters_ref':self.incoming['parameters_ref'],'method_ref':self.method_ref,
            'profile_bindings':self.bindings,'processing_state':'running','started_at':known(self.started),'finished_at':unknown('running'),
            'attempts':[{'attempt_no':1,'processing_state':'running','started_at':self.started,'finished_at':unknown('running'),
                'capture_log_ref':known(self.log_ref),'candidate_ref':known(self.candidate_ref),'receipt_ref':unknown('not_validated'),'failure_ref':na()}],
            'sealed_output_ref':unknown('not_sealed'),'failure_summary_ref':na(),
            'retention':{'state':'in_use','reason':'calibration evidence','required_by':['calibration']}}
        path = self.folder.parent/'manifest.json'
        manifest = json.loads(path.read_bytes()) if path.exists() else {'schema_version':1,'stage_id':'SC02','execution_date':self.day,'timezone':'Asia/Shanghai','invocations':[]}
        manifest['invocations'] = [v for v in manifest['invocations'] if v['invocation_id'] != self.invocation_id]+[invocation]
        path.write_bytes(canonical(manifest))

    def validate(self,label):
        command = [sys.executable,'-B',str(PLUGIN/'scripts/validate_sc02.py'),'--input',str(self.folder/'input.json'),
            '--candidate',str(self.attempt/'candidate.json'),'--root',str(self.root),'--run-id',self.run_id,
            '--invocation-id',self.invocation_id,'--attempt-no','1','--mode','test','--network-origin','simulated','--plugins-dir',str(self.plugins)]
        result = subprocess.run(command,cwd=PLUGIN,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8'),capture_output=True)
        (self.attempt/(label+'.stdout.json')).write_bytes(result.stdout);(self.attempt/(label+'.stderr.log')).write_bytes(result.stderr)
        self.assertIn(result.returncode,(0,1),result.stderr.decode(errors='replace'))
        receipt = json.loads(result.stdout);self.schemas.validate('investment.validation_receipt',receipt)
        return result.returncode,receipt

    def assert_rejected(self,code,receipt,rule):
        self.assertEqual(code,1,receipt)
        self.assertEqual(next(c for c in receipt['checks'] if c['rule_id'] == rule)['verdict'],'failed',receipt)

    def adopt_target(self,time):
        document = copy.deepcopy(self.expected_documents['source_b'])
        document.update(published_time=time,window_time=time,window_membership='uncertain' if time['status'] == 'unknown' else 'within')
        self.rows['documents'] = sorted([d for d in self.rows['documents'] if d['source_id'] != 'source_b']+[document],key=lambda d:(d['document_id'],d['version_id']))
        for o in self.rows['opportunity_inventory']:
            if o['source_id'] == 'source_b':o.update(disposition='captured',document_ids=[document['document_id']])
        for c in self.rows['coverage']:
            if c['source_id'] == 'source_b':c.update(status='partial',document_ids=[document['document_id']])
        self.refresh_gaps();self.save_candidate()

    def test_supported_material(self):
        for kind in ('html','xml','json'):
            with self.subTest(format=kind):
                self.build_case(kind=kind);code,receipt = self.validate('supported-'+kind)
                self.assertEqual(code,0,receipt);self.assertEqual(receipt['business_result'],'observation_only')
                body = json.loads((self.attempt/('body-'+self.material_records['source_a'][1]['capture_id']+'.json')).read_bytes())
                self.assertEqual(body['text'],self.specs['source_a']['text'])
                self.assertEqual(body['title'],self.specs['source_a']['title'])
                self.assertEqual(body['source_time'],self.specs['source_a']['literal'])
                self.assertEqual(body['body_locator']=='' ,kind=='json')
                with self.assertRaises(ContractError):
                    self.schemas.validate('investment.news_body',dict(body,body_locator='/properties' if kind == 'json' else ''))
                self.assertTrue(all(c['origin'] == 'synthetic' for _,c in self.captures))
                self.assertNotIn('decision_advice',receipt['allowed_uses'])
        for source_format in ('rss','api'):
            with self.subTest(publication=source_format):
                self.build_case(publication=source_format);code,receipt = self.validate('supported-'+source_format)
                self.assertEqual(code,0,receipt)
                document = next(d for d in self.rows['documents'] if d['source_id'] == 'source_b')
                self.assertEqual(document['published_time']['status'],'known')
                self.assertEqual(document['published_time']['value']['basis_ref'],self.listings['source_b'][0])
                self.assertEqual(self.expected_bodies['source_b']['source_time'],self.specs['source_b']['literal'])
                self.assertEqual(document['available_time']['status'],'unknown')
        self.build_case(video=True);code,receipt = self.validate('text-and-pending-video')
        self.assertEqual(code,0,receipt)
        self.assertEqual(len(self.rows['documents']),1)
        self.assertEqual(self.rows['pending_items'][0]['reason_code'],'pending_parse')

    def test_body_or_time_not_supported(self):
        self.build_case();original = copy.deepcopy(self.rows)
        raw_ref,capture = self.material_records['source_a']
        wrong = dict(self.expected_bodies['source_a'],text='A plausible copied paragraph from another release.')
        self.rows['documents'][0]['body_ref'] = self.put('body-'+capture['capture_id']+'.json',wrong,'investment.news_body')
        self.save_candidate();self.assert_rejected(*self.validate('wrong-body'),'N06')
        self.rows = original
        self.rows['documents'][0]['body_ref'] = self.put('body-'+capture['capture_id']+'.json',self.expected_bodies['source_a'],'investment.news_body')
        self.rows['documents'][0]['published_time']['value']['lower_bound'] = '2026-01-01T00:00:00.000000Z'
        self.save_candidate();self.assert_rejected(*self.validate('wrong-time'),'N07')
        self.build_case(video=True);code,receipt = self.validate('honest-video-pending')
        self.assertEqual(code,0,receipt)
        raw_ref,capture = self.video_record
        body = dict(self.expected_bodies['source_a'],capture_ref=raw_ref,title=self.video['title'],text=self.video['text'])
        body_ref = self.put('body-'+capture['capture_id']+'.json',body,'investment.news_body')
        document = dict(self.expected_documents['source_a'],document_id=identity('doc','source_a',capture['final_url']),
            version_id='ver-'+hashlib.sha256(self.video['raw']).hexdigest(),title=self.video['title'],
            requested_url=capture['requested_url'],final_url=capture['final_url'],raw_ref=raw_ref,body_ref=body_ref,retrieved_at=capture['response_finished_at'])
        self.rows['documents'].append(document);self.rows['documents'].sort(key=lambda d:(d['document_id'],d['version_id']))
        for o in self.rows['opportunity_inventory']:
            if o['disposition'] == 'pending_parse':o.update(disposition='captured',document_ids=[document['document_id']])
        self.rows['pending_items'] = []
        self.rows['coverage'][0].update(document_ids=sorted(d['document_id'] for d in self.rows['documents']),pending_ids=[],
            errors=[e for e in self.rows['coverage'][0]['errors'] if e['code'] != 'layout_unknown'])
        self.refresh_gaps();self.save_candidate();self.assert_rejected(*self.validate('video-credits-adopted'),'N06')
        for source_format in ('rss','api'):
            with self.subTest(outside_publication=source_format):
                self.build_case(publication=source_format,outside=True);code,receipt = self.validate('honest-outside-'+source_format)
                self.assertEqual(code,0,receipt)
                target = next(o for o in self.rows['opportunity_inventory'] if o['source_id'] == 'source_b')
                self.assertEqual(target['disposition'],'excluded_outside_window')
                self.adopt_target(unknown('source_timezone_missing'))
                self.assert_rejected(*self.validate('known-outside-adopted-uncertain-'+source_format),'N07')
        self.build_case(publication='rss',outside=True,conflict=True);code,receipt = self.validate('honest-conflicting-publication')
        self.assertEqual(code,0,receipt)
        target = next(d for d in self.rows['documents'] if d['source_id'] == 'source_b')
        self.assertEqual(target['published_time'],unknown('conflicting_publication_evidence'))
        self.assertTrue(any(g['reason'] == 'conflicting_publication_evidence' for g in receipt['blockers']))
        self.adopt_target(known({'lower_bound':'2026-10-18T12:00:00.000000Z','upper_bound':'2026-10-18T12:00:00.000000Z',
                                'precision':'instant','timezone':'UTC','basis_ref':self.listings['source_b'][0]}))
        self.assert_rejected(*self.validate('conflict-chosen-to-fit-window'),'N07')

    def test_missing_source_or_opportunity(self):
        self.build_case(failing=True,unfetched=True);original = copy.deepcopy(self.rows)
        self.rows['coverage'] = self.rows['coverage'][:1];self.save_candidate()
        self.assert_rejected(*self.validate('missing-source'),'N10')
        self.rows = original
        self.rows['opportunity_inventory'] = [o for o in self.rows['opportunity_inventory'] if o['disposition'] == 'captured']
        self.save_candidate();self.assert_rejected(*self.validate('missing-opportunity'),'N08')

    def test_partial_coverage_is_honest(self):
        self.build_case(failing=True);code,receipt = self.validate('partial')
        self.assertEqual(code,0,receipt);self.assertEqual(receipt['overall_verdict'],'partial')
        self.assertEqual(receipt['business_result'],'observation_only')
        self.assertEqual(receipt['allowed_uses'],['evidence_review','hypothesis_research','reporting'])
        self.assertEqual([c['source_id'] for c in self.rows['coverage']],['source_a','source_b'])
        self.assertEqual(self.rows['coverage'][1]['errors'][0]['code'],'request_failed')
        self.assertTrue(all(c['window_coverage'] == 'unknown' for c in self.rows['coverage']))
