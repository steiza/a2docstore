import base64
from datetime import datetime
import logging
import os
import shutil
import sys
from urllib import quote, unquote

from sqlalchemy import Column, create_engine, desc, Date, func, Integer, or_, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from tornado.ioloop import IOLoop
from tornado.web import Application, RequestHandler, StaticFileHandler, stream_request_body
import yaml


class IndexHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self):
        authorized = self.get_secure_cookie('authorized')

        session = self.__SessionMaker()
        recent_docs = session.query(DocModel).order_by(desc('date_uploaded')).limit(10)
        session.close()

        notification = self.get_cookie('notification')

        if notification:
            notification = unquote(notification)
            self.clear_cookie('notification')

        self.render(
            'index.html', region=self.__region,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized, recent_docs=recent_docs,
            notification=notification
            )


class AddHandler(RequestHandler):

    def initialize(
        self, region, google_analytics_id, SessionMaker, stored_docs_path):

        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker
        self.__stored_docs_path = stored_docs_path

    def get(self):
        authorized = self.get_secure_cookie('authorized')

        session = self.__SessionMaker()

        org_results = session.query(DocModel.source_org).group_by(
                DocModel.source_org).order_by(DocModel.source_org).all()

        org_names = [each[0] for each in org_results]

        session.close()

        self.render(
            'add.html', region=self.__region, org_names=org_names,
            google_analytics_id=self.__google_analytics_id, authorized=authorized
            )

    def post(self):
        new_doc = DocModel()

        # Get required arguments
        for each_property in ['doc_title', 'doc_description', 'source_org']:
            value = self.get_argument(each_property)
            setattr(new_doc, each_property, value)

        # Make sure file contents are there
        file_array = self.request.files.get('file', None)

        if not file_array or len(file_array) == 0:
            self.set_status(400)
            self.write('Request missing file upload')
            return

        file_data = file_array[0]['body']
        filename = file_array[0]['filename']

        # Get optional fields
        tracking_number = self.get_argument('tracking_number', default=None)
        new_doc.tracking_number = tracking_number

        date_requested = self.get_argument('date_requested', default=None)
        if date_requested:
            date_requested = datetime.strptime(date_requested, '%m/%d/%Y').date()
        else:
            date_requested = None

        new_doc.date_requested = date_requested

        date_received = self.get_argument('date_received', default=None)
        if date_received:
            date_received = datetime.strptime(date_received, '%m/%d/%Y').date()
        else:
            date_received = None

        new_doc.date_received = date_received

        new_doc.uploader_name = self.get_argument(
            'uploader_name') or 'anonymous'

        new_doc.uploader_email = self.get_argument(
            'uploader_email') or 'anonymous@example.com'

        # Set metadata
        new_doc.date_uploaded = datetime.now().date()
        new_doc.filename = filename

        # Save document in sqlite to get id number
        session = self.__SessionMaker()
        session.add(new_doc)
        session.commit()

        document_id = new_doc.id
        session.close()

        # Use id number to write to disk
        file_path = os.path.join(self.__stored_docs_path, str(document_id), filename)

        os.mkdir(os.path.dirname(file_path))

        fd = open(file_path, 'w')
        fd.write(file_data)
        fd.close()

        self.set_cookie('notification', quote('Document added; thanks!'))
        self.redirect('/')


class SearchHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self):
        authorized = self.get_secure_cookie('authorized')

        query_arg = self.get_argument('query', default=None)
        offset = self.get_argument('offset', default=None)

        if not offset:
            offset = 0
        else:
            offset = int(offset)

        session = self.__SessionMaker()
        query = session.query(DocModel)

        if query_arg:
            query = query.filter(or_(
                DocModel.doc_title.like('%%%s%%' % query_arg),
                DocModel.doc_description.like('%%%s%%' % query_arg),
                DocModel.source_org.like('%%%s%%' % query_arg),
                DocModel.uploader_name.like('%%%s%%' % query_arg)
                ))
        else:
            query_arg = ''

        query = query.order_by(desc(DocModel.date_uploaded))

        count = query.count()

        matching_docs = query.offset(offset).limit(20).all()

        session.close()

        self.render(
            'search.html', region=self.__region,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized, query=query_arg, offset=offset, count=count,
            matching_docs=matching_docs
            )


class ViewHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self, document_id):
        authorized = self.get_secure_cookie('authorized')

        session = self.__SessionMaker()
        doc = session.query(DocModel).filter(DocModel.id == document_id).one()
        session.close()

        self.render(
            'view.html', region=self.__region,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized, doc=doc
            )


class DownloadHandler(RequestHandler):

    def initialize(self, stored_docs_path):
        self.__stored_docs_path = stored_docs_path

    def get(self, doc_id, filename):
        file_path = os.path.join(self.__stored_docs_path, str(doc_id), filename)

        if not os.path.exists(file_path):
            self.set_status(404)
            self.write('File not found')

        self.set_header('Content-Type', 'application/ocet-stream')
        self.set_header('Content-Disposition', 'attachment; filename={0}'.format(filename))

        fd = open(file_path, 'r')

        while True:
            chunk = fd.read(1000000)

            if chunk:
                self.write(chunk)

            else:
                fd.close()
                break


class EditHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self, doc_id):
        authorized = self.get_secure_cookie('authorized')

        if not authorized:
            self.set_status(400)
            self.write('Not authorized')
            return

        session = self.__SessionMaker()

        doc = session.query(DocModel).filter(DocModel.id == doc_id).one()

        org_results = session.query(DocModel.source_org).group_by(
                DocModel.source_org).order_by(DocModel.source_org).all()

        org_names = [each[0] for each in org_results]

        session.close()

        # Make sure dates are in the form understood by JavaScript
        if doc.date_requested:
            doc.date_requested = doc.date_requested.strftime('%m/%d/%Y')

        if doc.date_received:
            doc.date_received = doc.date_received.strftime('%m/%d/%Y')

        self.render(
            'edit.html', region=self.__region, org_names=org_names,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized, doc=doc
            )

    def post(self, doc_id):
        session = self.__SessionMaker()
        doc = session.query(DocModel).filter(DocModel.id == doc_id).one()

        for each_property in ['doc_title', 'doc_description', 'source_org',
            'tracking_number', 'date_requested', 'date_received',
            'uploader_name', 'uploader_email']:

            value = self.get_argument(each_property)

            if each_property.startswith('date_'):
                if value:
                    value = datetime.strptime(value, '%m/%d/%Y').date()
                else:
                    value = None

            setattr(doc, each_property, value)

        session.add(doc)
        session.commit()

        self.set_cookie('notification', quote('Document updated; thanks!'))
        self.redirect('/')


class DeleteHandler(RequestHandler):

    def initialize(self, SessionMaker, stored_docs_path):
        self.__SessionMaker = SessionMaker
        self.__stored_docs_path = stored_docs_path

    def post(self):
        # Make sure we are authorized
        authorized = self.get_secure_cookie('authorized')

        if not authorized:
            self.set_status(400)
            self.write('Not authorized')
            return

        doc_id = self.get_argument('doc_id', None)

        if not doc_id:
            self.set_stataus(401)
            self.write('Bad request, no doc_id')
            return

        # Remove file on disk
        shutil.rmtree(os.path.join(self.__stored_docs_path, str(doc_id)))

        # Remove metadata
        session = self.__SessionMaker()
        doc = session.query(DocModel).filter(DocModel.id == doc_id).one()
        session.delete(doc)
        session.commit()

        self.set_cookie(
            'notification',
            quote('Deleted document: {}"'.format(doc.doc_title.encode('utf8')))
            )

        self.write({'success': True})


class OrgHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self):
        authorized = self.get_secure_cookie('authorized')

        session = self.__SessionMaker()

        org_results = session.query(
            DocModel.source_org, func.count(DocModel.source_org)).group_by(
                DocModel.source_org).order_by(DocModel.source_org).all()

        session.close()

        self.render(
            'org.html', region=self.__region,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized,
            org_results=org_results
            )


class SubmitterHandler(RequestHandler):

    def initialize(self, region, google_analytics_id, SessionMaker):
        self.__region = region
        self.__google_analytics_id = google_analytics_id
        self.__SessionMaker = SessionMaker

    def get(self):
        authorized = self.get_secure_cookie('authorized')

        session = self.__SessionMaker()

        submitter_results = session.query(
            DocModel.uploader_name, func.count(DocModel.uploader_name)).group_by(
                DocModel.uploader_name).order_by(DocModel.uploader_name).all()

        session.close()

        self.render(
            'submitter.html', region=self.__region,
            google_analytics_id=self.__google_analytics_id,
            authorized=authorized,
            submitter_results=submitter_results,
            )


class AuthHandler(RequestHandler):

    def initialize(self, password):
        self.__password = password

    def get(self):
        # First, check if this is a log out request
        log_out = self.get_argument('log_out', default=None)

        if log_out:
            self.clear_cookie('authorized')
            self.set_cookie('notification', quote('You have signed out'))
            self.redirect('/')
            return

        # If not, make sure basic auth is submitted
        auth_header = self.request.headers.get('Authorization')

        if not auth_header:
            self.set_header('WWW-Authenticate', 'Basic realm=/auth/')
            self.set_status(401)
            return

        else:
            # We have basic auth info; check it
            auth_decoded = base64.decodestring(auth_header[6:])
            username, password = auth_decoded.split(':', 2)

            if password == self.__password:
                self.set_secure_cookie('authorized', 'true')
                self.set_cookie('notification', quote('You have signed in succesfully!'))
                self.redirect('/')

            else:
                self.set_header('WWW-Authenticate', 'Basic realm=/auth/')
                self.set_status(401)
                return


Base = declarative_base()

class DocModel(Base):
    __tablename__ = 'docs'

    id = Column(Integer, primary_key=True)
    doc_title = Column(String)
    doc_description = Column(String)
    source_org = Column(String)
    tracking_number = Column(String, nullable=True)
    date_requested = Column(Date, nullable=True)
    date_received = Column(Date, nullable=True)
    uploader_name = Column(String)
    uploader_email = Column(String)
    filename = Column(String)
    date_uploaded = Column(Date)


if __name__ == '__main__':
    current_directory = os.path.dirname(__file__)
    static_path = os.path.join(current_directory, 'static')
    template_path = os.path.join(current_directory, 'templates')
    stored_docs_path = os.path.join(current_directory, 'storage', 'docs')

    engine = create_engine('sqlite:///storage/db/sqlite.db')
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(engine)

    if not os.path.exists('settings.yml'):
        print ''
        print 'Error: application needs settings.yml file to run'
        print ''
        print 'For details see settings.sample.yml or README.rst'
        print ''
        sys.exit(-1)

    with open('settings.yml', 'r') as fd:
        settings = yaml.load(fd)

    google_analytics_id = settings.get('google_analytics_id', None)

    loggers = ['tornado.access', 'tornado.application', 'tornado.general']

    for each_logger in loggers:
        logger = logging.getLogger(each_logger)
        logger.setLevel(logging.DEBUG)

        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

        logger.addHandler(handler)

    app = Application([
        (r'/static/(.*)', StaticFileHandler, {'path': static_path}),

        (r'/', IndexHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/add', AddHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker,
            stored_docs_path=stored_docs_path
            )),

        (r'/search', SearchHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/view/([0-9]+)', ViewHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/file/([0-9]+)/(.*)', DownloadHandler, dict(
            stored_docs_path=stored_docs_path
            )),

        (r'/edit/([0-9]+)', EditHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/delete', DeleteHandler, dict(
            SessionMaker=SessionMaker,
            stored_docs_path=stored_docs_path
            )),

        (r'/orgs', OrgHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/submitters', SubmitterHandler, dict(
            region=settings['region'],
            google_analytics_id=google_analytics_id,
            SessionMaker=SessionMaker
            )),

        (r'/auth/', AuthHandler, dict(
            password=settings['password']
            ))

        ],

        template_path=template_path,
        cookie_secret=settings['cookie_secret'],
        xsrf_cookies=True
        )

    app.listen(8000)

    IOLoop.current().start()
