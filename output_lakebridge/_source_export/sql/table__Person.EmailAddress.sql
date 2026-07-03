CREATE TABLE [Person].[EmailAddress] (
    [BusinessEntityID] int NOT NULL,
    [EmailAddressID] int NOT NULL,
    [EmailAddress] nvarchar(50) NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
