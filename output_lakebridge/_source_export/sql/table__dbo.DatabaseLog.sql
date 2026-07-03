CREATE TABLE [dbo].[DatabaseLog] (
    [DatabaseLogID] int NOT NULL,
    [PostTime] datetime NOT NULL,
    [DatabaseUser] nvarchar(128) NOT NULL,
    [Event] nvarchar(128) NOT NULL,
    [Schema] nvarchar(128) NULL,
    [Object] nvarchar(128) NULL,
    [TSQL] nvarchar(MAX) NOT NULL,
    [XmlEvent] xml NOT NULL
);
