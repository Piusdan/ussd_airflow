"""
Comming soon
"""
from urllib.parse import unquote
from copy import copy, deepcopy
from rest_framework.views import APIView
from django.http import HttpResponse
from structlog import get_logger
import staticconf
from django.conf import settings
from importlib import import_module
from django.contrib.sessions.backends import signed_cookies
from django.contrib.sessions.backends.base import CreateError
from jinja2 import Template, Environment, TemplateSyntaxError
from .screens.serializers import UssdBaseSerializer
from rest_framework.serializers import SerializerMetaclass
import re
import json
import os
from configure import Configuration

_registered_ussd_handlers = {}


class MissingAttribute(Exception):
    pass


class InvalidAttribute(Exception):
    pass


class DuplicateSessionId(Exception):
    pass


def ussd_session(session_id):
    # Make a session storage
    session_engine = import_module(getattr(settings, "USSD_SESSION_ENGINE", settings.SESSION_ENGINE))
    if session_engine is signed_cookies:
        raise ValueError("You cannot use channels session functionality with signed cookie sessions!")
    # Force the instance to load in case it resets the session when it does
    session = session_engine.SessionStore(session_key=session_id)
    session._session.keys()
    session._session_key = session_id

    # If the session does not already exist, save to force our
    # session key to be valid.
    if not session.exists(session.session_key):
        try:
            session.save(must_create=True)
        except CreateError:
            # Session wasn't unique, so another consumer is doing the same thing
            raise DuplicateSessionId("another sever is working"
                                     "on this session id")
    return session


def load_variables(file_path, namespace):
    file_path = Template(file_path).render(os.environ)
    variables = dict(
        Configuration.from_file(os.path.abspath(file_path)).configure()
    )
    staticconf.DictConfiguration(
        variables,
        namespace=namespace,
        flatten=False)


def load_ussd_screen(file_path, namespace):
    staticconf.YamlConfiguration(
        os.path.abspath(file_path),
        namespace=namespace,
        flatten=False)


class UssdRequest(object):
    """
    :param session_id:
        used to get session or create session if does not
        exits.

        If session is less than 8 we add *s* to make the session
        equal to 8

    :param phone_number:
        This the user identifier

    :param input:
        This ussd input the user has entered.

    :param language:
        Language to use to display ussd

    :param kwargs:
        Extra arguments.
        All the extra arguments will be set to the self attribute

        For instance:

        .. code-block:: python

            from ussd.core import UssdRequest

            ussdRequest = UssdRequest(
                '12345678', '702729654', '1', 'en',
                name='mwas'
            )

            # accessing kwarg argument
            ussdRequest.name
    """
    def __init__(self, session_id, phone_number,
                 ussd_input, language, default_language=None, **kwargs):
        """Represents a USSD request"""

        self.phone_number = phone_number
        self.input = unquote(ussd_input)
        self.language = language
        self.default_language = default_language or 'en'
        # if session id is less than 8 should provide the
        # suplimentary characters with 's'
        if len(str(session_id)) < 8:
            session_id = 's' * (8 - len(str(session_id))) + session_id
        self.session_id = session_id
        self.session = ussd_session(self.session_id)

        for key, value in kwargs.items():
            setattr(self, key, value)


    def forward(self, handler_name):
        """
        Forwards a copy of the current request to a new
        handler. Clears any input, as it is assumed this was meant for
        the previous handler. If you need to pass info between
        handlers, do it through the USSD session.
        """
        new_request = copy(self)
        new_request.input = ''
        return new_request, handler_name

    def all_variables(self):
        all_variables = deepcopy(self.__dict__)

        # delete session if it exist
        all_variables.pop("session", None)

        return all_variables


class UssdResponse(object):
    """
    :param text:
        This is the ussd text to display to the user
    :param status:
        This shows the status of ussd session.

        True -> to continue with the session

        False -> to end the session
    :param session:
        This is the session object of the ussd session
    """
    def __init__(self, text, status=True, session=None):
        self.text = text
        self.status = status
        self.session = session

    def dumps(self):
        return self.text

    def __str__(self):
        return self.dumps()


class UssdHandlerMetaClass(type):

    def __init__(cls, name, bases, attr, **kwargs):
        super(UssdHandlerMetaClass, cls).__init__(
            name, bases, attr)

        abstract = attr.get('abstract', False)

        if not abstract:
            required_attributes = ('screen_type', 'serializer', 'handle')

            # check all attributes have been defined
            for attribute in required_attributes:
                if attribute not in attr and not hasattr(cls, attribute):
                    raise MissingAttribute(
                        "{0} is required in class {1}".format(
                            attribute, name)
                    )

            if not isinstance(attr['serializer'], SerializerMetaclass):
                raise InvalidAttribute(
                    "serializer should be a "
                    "instance of {serializer}".format(
                        serializer=SerializerMetaclass)
                )
            _registered_ussd_handlers[attr['screen_type']] = cls


class UssdHandlerAbstract(object, metaclass=UssdHandlerMetaClass):
    abstract = True

    def __init__(self, ussd_request: UssdRequest,
                 handler: str, screen_content: dict,
                 template_namespace=None, logger=None):
        self.ussd_request = ussd_request
        self.handler = handler
        self.screen_content = screen_content

        self.SINGLE_VAR = re.compile(r"^%s\s*(\w*)\s*%s$" % (
            '{{', '}}'))
        self.clean_regex = re.compile(r'^{{\s*(\S*)\s*}}$')
        self.logger = logger or get_logger(__name__).bind(
            **ussd_request.all_variables())
        self.template_namespace = template_namespace

        if template_namespace is not None:
            self.template_namespace = staticconf.config.\
                configuration_namespaces[self.template_namespace].\
                configuration_values

    def _get_session_items(self) -> dict:
        return dict(iter(self.ussd_request.session.items()))

    def _get_context(self, extra_context=None):
        context = self._get_session_items()
        context.update(
            dict(
                ussd_request=self.ussd_request.all_variables()
            )
        )
        context.update(
            dict(os.environ)
        )
        if self.template_namespace:
            context.update(self.template_namespace)
        if extra_context is not None:
            context.update(extra_context)
        return context

    def _render_text(self, text, context=None, extra=None, encode=None):
        if context is None:
            context = self._get_context()

        if extra:
            context.update(extra)

        template = Template(text or '', keep_trailing_newline=True)
        text = template.render(context)
        return json.dumps(text) if encode is 'json' else text

    def get_text(self, text_context=None):
        text_context = self.screen_content.get('text')\
                       if text_context is None \
                       else text_context

        if isinstance(text_context, dict):
            language = self.ussd_request.language \
                   if self.ussd_request.language \
                          in text_context.keys() \
                   else self.ussd_request.default_language

            text_context = text_context[language]

        return self._render_text(
            text_context
        )

    def evaluate_jija_expression(self, expression, extra_context=None):

        if isinstance(expression, str):
            context = self._get_context(extra_context=extra_context)
            expression = expression.replace("{{", "").replace("}}", "")
            try:
                env = Environment()
                expr = env.compile_expression(
                    expression
                )
            except TemplateSyntaxError:
                return []
            return expr(context)
        return expression

    @classmethod
    def validate(cls, screen_name: str, ussd_content: dict) -> (bool, dict):
        screen_content = ussd_content[screen_name]

        validation = cls.serializer(data=screen_content,
                                     context=ussd_content)

        if validation.is_valid():
            return True, {}
        return False, validation.errors

    @staticmethod
    def _contains_vars(data):
        '''
        returns True if the data contains a variable pattern
        '''
        if isinstance(data, str):
            for marker in ('{%', '{{', '{#'):
                if marker in data:
                    return True
        return False


class UssdView(APIView):
    """
    To create Ussd View requires the following things:
        - Inherit from **UssdView** (Mandatory)
            .. code-block:: python

                from ussd.core import UssdView

        - Define Http method either **get** or **post** (Mandatory)
            The http method should return Ussd Request

                .. autoclass:: ussd.core.UssdRequest

        - define this varialbe *customer_journey_conf*
            This is the path of the file that has ussd screens
            If you want your file to be dynamic implement the
            following method **get_customer_journey_conf** it
            will be called by request object

        - define this variable *customer_journey_namespace*
            Ussd_airflow uses this namespace to save the
            customer journey content in memory. If you want
            customer_journey_namespace to be dynamic implement
            this method **get_customer_journey_namespace** it
            will be called with request object

        - override HttpResponse
            In ussd airflow the http method return UssdRequest object
            not Http response. Then ussd view gets UssdResponse object
            and convert it to HttpResponse. The default HttpResponse
            returned is a normal HttpResponse with body being ussd text

            To override HttpResponse returned define this method.
            **ussd_response_handler** it will be called with
            **UssdResponse** object.

                .. autoclass:: ussd.core.UssdResponse

    Example of Ussd view

    .. code-block:: python

        from ussd.core import UssdView, UssdRequest


        class SampleOne(UssdView):

            def get(self, req):
                return UssdRequest(
                    phone_number=req.data['phoneNumber'].strip('+'),
                    session_id=req.data['sessionId'],
                    ussd_input=text,
                    service_code=req.data['serviceCode'],
                    language=req.data.get('language', 'en')
                )

    Example of Ussd View that defines its own HttpResponse.

    .. code-block:: python

        from ussd.core import UssdView, UssdRequest


        class SampleOne(UssdView):

            def get(self, req):
                return UssdRequest(
                    phone_number=req.data['phoneNumber'].strip('+'),
                    session_id=req.data['sessionId'],
                    ussd_input=text,
                    service_code=req.data['serviceCode'],
                    language=req.data.get('language', 'en')
                )

            def ussd_response_handler(self, ussd_response):
                    if ussd_response.status:
                        res = 'CON' + ' ' + str(ussd_response)
                        response = HttpResponse(res)
                    else:
                        res = 'END' + ' ' + str(ussd_response)
                        response = HttpResponse(res)
                    return response
    """
    customer_journey_conf = None
    customer_journey_namespace = None
    template_namespace = None

    def initial(self, request, *args, **kwargs):
        # initialize restframework
        super(UssdView, self).initial(request, args, kwargs)

        # initialize ussd
        self.ussd_initial(request)

    def ussd_initial(self, request, *args, **kwargs):
        if hasattr(self, 'get_customer_journey_conf'):
            self.customer_journey_conf = self.get_customer_journey_conf(
                request
            )
        if hasattr(self, 'get_customer_journey_namespace'):
            self.customer_journey_namespace = \
                self.get_customer_journey_namespace(request)

        if self.customer_journey_conf is None \
                or self.customer_journey_namespace is None:
            raise MissingAttribute("attribute customer_journey_conf and "
                                   "customer_journey_namespace are required")

        if not self.customer_journey_namespace in \
                staticconf.config.configuration_namespaces:
            load_ussd_screen(
                self.customer_journey_conf,
                self.customer_journey_namespace
            )

        # check if variables exit and have been loaded
        initial_screen = staticconf.read(
            'initial_screen',
            namespace=self.customer_journey_namespace)

        if isinstance(initial_screen, dict) and initial_screen.get('variables'):
            variable_conf = initial_screen['variables']
            file_path = variable_conf['file']
            namespace = variable_conf['namespace']

            # check if it has been loaded
            if not namespace in \
                    staticconf.config.configuration_namespaces:
                load_variables(file_path, namespace)
            self.template_namespace = namespace

    def finalize_response(self, request, response, *args, **kwargs):

        if isinstance(response, UssdRequest):
            self.logger = get_logger(__name__).bind(**response.all_variables())
            ussd_response = self.ussd_dispatcher(response)
            return self.ussd_response_handler(ussd_response)
        return super(UssdView, self).finalize_response(
            request, response, args, kwargs)

    def ussd_response_handler(self, ussd_response):
        return HttpResponse(str(ussd_response))

    def ussd_dispatcher(self, ussd_request):

        # Clear input and initialize session if we are starting up
        if '_ussd_state' not in ussd_request.session:
            ussd_request.input = ''
            ussd_request.session['_ussd_state'] = {'next_screen': ''}
            ussd_request.session['steps'] = []
            ussd_request.session['posted'] = False
            ussd_request.session['submit_data'] = {}
            ussd_request.session['session_id'] = ussd_request.session_id
            ussd_request.session['phone_number'] = ussd_request.phone_number

        ussd_request.session.update(ussd_request.all_variables())

        self.logger.debug('gateway_request', text=ussd_request.input)

        # Invoke handlers
        ussd_response = self.run_handlers(ussd_request)
        # Save session
        ussd_request.session.save()
        self.logger.debug('gateway_response', text=ussd_response.dumps(),
                     input="{redacted}")

        return ussd_response

    def run_handlers(self, ussd_request):
        if ussd_request.session['_ussd_state']['next_screen']:
            handler = ussd_request.session['_ussd_state']['next_screen']
        else:
            handler = staticconf.read(
                'initial_screen', namespace=self.customer_journey_namespace)
            if isinstance(handler, dict):
                # set default language from namespace
                if 'default_language' in handler:
                    ussd_request.default_language = handler.get('default_language', ussd_request.default_language)
                handler = handler["screen"]
        ussd_response = (ussd_request, handler)

        # Handle any forwarded Requests; loop until a Response is
        # eventually returned.
        while not isinstance(ussd_response, UssdResponse):
            ussd_request, handler = ussd_response

            screen_content = staticconf.read(
                handler,
                namespace=self.customer_journey_namespace)

            ussd_response = _registered_ussd_handlers[screen_content['type']](
                ussd_request,
                handler,
                screen_content,
                template_namespace=self.template_namespace,
                logger=self.logger
            ).handle()

        ussd_request.session['_ussd_state']['next_screen'] = handler


        # Attach session to outgoing response
        ussd_response.session = ussd_request.session

        return ussd_response

    @staticmethod
    def validate_ussd_journey(ussd_content: dict) -> (bool, dict):
        errors = {}
        is_valid = True

        # should define initial screen
        if not 'initial_screen' in ussd_content.keys():
            is_valid = False
            errors.update(
                {'hidden_fields': {
                    "initial_screen": ["This field is required."]
                }}
            )
        for screen_name, screen_content in ussd_content.items():
            # all screens should have type attribute
            if screen_name == "initial_screen":
                # confirm the next screen is in the screen content
                if isinstance(screen_content, dict):
                    screen_content = screen_content.get('screen')
                if not screen_content in ussd_content.keys():
                    is_valid = False
                    errors.update(
                        dict(
                            screen_name="Screen not available"
                        )
                    )
                continue

            screen_type = screen_content.get('type')

            # all screen should have type field.
            serialize = UssdBaseSerializer(data=screen_content,
                                           context=ussd_content)
            base_validation = serialize.is_valid()

            if serialize.errors:
                errors.update(
                    {screen_name: serialize.errors}
                )

            if not base_validation:
                is_valid = False
                continue

            # all screen type have their handlers
            handlers = _registered_ussd_handlers[screen_type]

            screen_validation, screen_errors = handlers.validate(
                screen_name,
                ussd_content
            )
            if screen_errors:
                errors.update(
                    {screen_name: screen_errors}
                )

            if not screen_validation:
                is_valid = screen_validation

        return is_valid, errors