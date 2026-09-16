"""Service clients for SJTU systems."""
from .door import DoorClient, get_truncated_room_id
from .pe import PEClient, LocationType
from .video import VideoClient
from .canvas import CanvasClient
from .checkin import CheckinClient
from .library import LibrarySeatClient, LibrarySeatAdvancedClient
from .sports import SportsReservationClient, SportsAPIError
from .questionnaire import QuestionnaireClient, QuestionnaireAPIError
