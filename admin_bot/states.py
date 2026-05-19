from aiogram.fsm.state import State, StatesGroup


class MassProfileUpdateStates(StatesGroup):
	WaitingForFirstName = State()
	WaitingForLastName = State()
	WaitingForBio = State()
	WaitingForPhoto = State()
	WaitingForUsernameChoice = State()
	ConfirmUpdate = State()


class AIConfigStates(StatesGroup):
	WaitingForBasePrompt = State()
	WaitingForOpenAIModel = State()
	WaitingForG4FModel = State()
	WaitingForTemperature = State()
	WaitingForMaxTokens = State()
	WaitingForConvPrompt = State()
	WaitingForConvMaxTokens = State()
	WaitingForConvMaxTurns = State()
	WaitingForNewOpenAIKey = State()
	ChoosingOpenAIKeyToDelete = State()
	ChoosingOpenAIKeyToToggle = State()
	WaitingForWelcomePrompt = State()

class TemplatesState(StatesGroup):
	new_template = State()

class AccountAdditionStates(StatesGroup):
	WaitingForPhone = State()
	WaitingForApiId = State()
	WaitingForApiHash = State()
	WaitingForLabel = State()
	WaitingForProxyChoice = State()
	WaitingForProxyType = State()
	WaitingForProxyIP = State()
	WaitingForProxyPort = State()
	WaitingForProxyUsername = State()
	WaitingForProxyPassword = State()
	WaitingForCode = State()
	WaitingFor2FAPassword = State()
	WaitingForTDataLabel = State()
	WaitingForTDataApiId = State()
	WaitingForTDataApiHash = State()
	WaitingForTDataConfirmation = State()
	WaitingForProxyFile = State()


class AccountSettingsStates(StatesGroup):
	WaitingForDMAutoReplyMessage = State()


class ChannelManagementStates(StatesGroup):
	WaitingForChannelIdentifier = State()
	WaitingForLinkedChatManual = State()
	WaitingForChannelIdToRemove = State()
	WaitingForBulkChannelFile = State()


class GroupManagementStates(StatesGroup):
	WaitingForAddGroup = State()
	WaitingForBulkGroupFile = State()
	WaitingForRemoveSingleGroup = State()
	WaitingForBulkRemoveFile = State()


class AccountManagementStates(StatesGroup):
	ShowingAccountMenu = State()
	ManagingProxy = State()
	WaitingForProfileFirstName = State()
	WaitingForProfileLastName = State()
	WaitingForProfileBio = State()
	WaitingForProfilePhoto = State()
	WaitingForProxyTypeUpdate = State()
	WaitingForProxyIPUpdate = State()
	WaitingForProxyPortUpdate = State()
	WaitingForProxyUsernameUpdate = State()
	WaitingForProxyPasswordUpdate = State()


class GeneralSettingsStates(StatesGroup):
	Menu = State()
	WaitingForWorkspaceName = State()


class CategoryRegularCommentStates(StatesGroup):
	WaitingForInterval = State()
	WaitingForPrompt = State()
